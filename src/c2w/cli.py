"""``c2w-admin`` -- operator command line.

This is how the platform is configured, because configuration lives in the
database rather than in files.  It is also the only way a credential should
ever be entered: values are prompted for without echo and sealed before they
touch storage, so they never appear in shell history, a config file, or a
process listing.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from c2w import __version__
from c2w.auth.local import AuthError, create_super_admin, hash_password, revoke_all_sessions
from c2w.config import get_bootstrap
from c2w.crypto import CryptoError, generate_data_key, seal
from c2w.db.models.auth import User
from c2w.db.models.core import Brand, CommPeakConnection, StorageDestination, Tenant
from c2w.db.session import dispose_engine, platform_session
from c2w.logging import configure_logging
from c2w.settings import SettingsError, settings_service
from c2w.settings_spec import SETTINGS, specs_by_category
from c2w.storage.commpeak import COMMPEAK_ENDPOINT

EXIT_OK, EXIT_ERROR = 0, 1


def _out(message: str = "") -> None:
    print(message)


async def _scope_to_brand(session, brand_id: int) -> None:
    """Scope the session to one brand for the rest of this transaction.

    Brand-scoped tables are protected by RLS, so an insert needs the scope set
    even when the caller is an administrator.  Setting it explicitly means these
    commands work as the least-privileged application role and do not depend on
    BYPASSRLS -- a privilege worth not needing.
    """
    await session.execute(
        text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
    )


async def _scope_all_brands(session) -> list[int]:
    """Report which brands this role can actually see.

    A cross-brand listing needs either BYPASSRLS or one query per brand.  Rather
    than silently returning an empty list when the role lacks the privilege --
    which reads as "nothing configured" -- callers use this to iterate brands
    explicitly.
    """
    ids = (await session.execute(text("SELECT id FROM brands ORDER BY id"))).scalars().all()
    return list(ids)


# ---------------------------------------------------------------- super admin


async def cmd_superadmin_create(args: argparse.Namespace) -> int:
    """Create the local super admin.

    Local accounts exist so the platform can be administered from the first
    minute, before Active Directory is connected.  Afterwards this account
    remains the break-glass route in, which is why it is the one role that can
    still sign in locally once `auth.local_accounts_enabled` is turned off.
    """
    password = args.password or getpass.getpass("Password (min 12 chars): ")
    if not args.password:
        if password != getpass.getpass("Confirm password: "):
            _out("Passwords do not match.")
            return EXIT_ERROR

    async with platform_session() as session:
        try:
            user = await create_super_admin(session, args.email, password, display_name=args.name)
        except AuthError as exc:
            _out(f"Could not create super admin: {exc}")
            return EXIT_ERROR
        _out(f"Created super admin {user.email} (id {user.id}).")
        _out("Authentication will move to Active Directory / Entra ID later; keep this")
        _out("account as the break-glass login and use a password manager for it.")
    return EXIT_OK


async def cmd_superadmin_passwd(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("New password (min 12 chars): ")
    async with platform_session() as session:
        user = (
            await session.execute(select(User).where(User.email == args.email.strip().lower()))
        ).scalar_one_or_none()
        if user is None:
            _out(f"No such user: {args.email}")
            return EXIT_ERROR
        try:
            user.password_hash = hash_password(password)
        except AuthError as exc:
            _out(str(exc))
            return EXIT_ERROR
        user.failed_logins = 0
        user.locked_until = None
        # A password change must end existing sessions, or a stolen cookie
        # outlives the credential it was issued against.
        await revoke_all_sessions(session, user.id)
        _out(f"Password updated for {user.email}; all sessions revoked.")
    return EXIT_OK


async def cmd_user_list(_args: argparse.Namespace) -> int:
    async with platform_session() as session:
        rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
        if not rows:
            _out("No users yet. Create one with:")
            _out("  c2w-admin superadmin create --email you@example.com")
            return EXIT_OK
        _out(f"{'ID':>4}  {'EMAIL':38} {'ROLE':16} {'SOURCE':7} {'BRAND':>6} ACTIVE")
        for u in rows:
            _out(
                f"{u.id:>4}  {u.email:38} {u.role:16} {u.auth_source:7} "
                f"{u.brand_id or '-':>6} {'yes' if u.is_active else 'no'}"
            )
    return EXIT_OK


# ------------------------------------------------------------------- settings


async def cmd_settings_list(args: argparse.Namespace) -> int:
    """Show effective settings.  Secrets are shown as configured/not, never as values."""
    async with platform_session() as session:
        effective = await settings_service.all_effective(session, brand_id=args.brand)
        for category, specs in specs_by_category().items():
            if args.category and args.category.lower() != category.lower():
                continue
            _out(f"\n=== {category} ===")
            for spec in specs:
                value = effective[spec.key]
                if spec.sensitive:
                    shown = "<configured>" if value else "<not set>"
                else:
                    shown = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
                unit = f" {spec.unit}" if spec.unit else ""
                default = "  (changed)" if value != spec.default else ""
                _out(f"  {spec.label:46} {shown}{unit}{default}")
                _out(f"      {spec.key}")
                if args.verbose:
                    scope = "  [per-organisation]" if spec.brand_overridable else ""
                    _out(f"      {spec.description}{scope}")
    return EXIT_OK


async def cmd_settings_get(args: argparse.Namespace) -> int:
    async with platform_session() as session:
        spec = SETTINGS.get(args.key)
        if spec is None:
            _out(f"Unknown setting: {args.key}")
            return EXIT_ERROR
        if spec.sensitive:
            value = await settings_service.get_secret(session, args.key, brand_id=args.brand)
            _out("<configured>" if value else "<not set>")
        else:
            value = await settings_service.get(session, args.key, brand_id=args.brand)
            _out(json.dumps(value) if isinstance(value, (list, dict)) else str(value))
    return EXIT_OK


async def cmd_settings_set(args: argparse.Namespace) -> int:
    spec = SETTINGS.get(args.key)
    if spec is None:
        _out(f"Unknown setting: {args.key}")
        _out("List available settings with: c2w-admin settings list")
        return EXIT_ERROR

    value: Any = args.value
    if spec.sensitive and value is None:
        # Prompted without echo so the secret never enters shell history.
        value = getpass.getpass(f"{args.key}: ")
    if value is None:
        _out(f"A value is required for {args.key}")
        return EXIT_ERROR

    async with platform_session() as session:
        try:
            stored = await settings_service.set(
                session, args.key, value, brand_id=args.brand, changed_by=args.actor or "cli"
            )
        except (SettingsError, CryptoError) as exc:
            _out(f"Rejected: {exc}")
            return EXIT_ERROR
        shown = "<configured>" if spec.sensitive else stored
        scope = f"brand {args.brand}" if args.brand else "global"
        _out(f"Set {args.key} = {shown} ({scope})")
        if spec.restart_required:
            _out("This setting takes effect after a service restart.")
    return EXIT_OK


async def cmd_settings_reveal(args: argparse.Namespace) -> int:
    """Print one setting in clear text, for handing to another process.

    Deliberately narrow. Only settings whose spec sets ``shell_exportable`` may
    be revealed, so this cannot become a way to dump CommPeak or Wasabi
    credentials out of the database. Output is the bare value with no label, so
    it can be used directly:

        export SHADCNIO_TOKEN=$(c2w-admin settings reveal integrations.shadcn_mcp_token)
    """
    spec = SETTINGS.get(args.key)
    if spec is None:
        _out(f"Unknown setting: {args.key}")
        return EXIT_ERROR
    if not spec.shell_exportable:
        _out(f"{args.key} may not be revealed.")
        _out("Only settings marked shell_exportable can be printed in clear text;")
        _out("stored credentials are readable by the application, not by this command.")
        return EXIT_ERROR

    async with platform_session() as session:
        value = await settings_service.get_secret(session, args.key, brand_id=args.brand)
        if not value:
            _out("")
            return EXIT_ERROR
        print(value)
    return EXIT_OK


async def cmd_settings_unset(args: argparse.Namespace) -> int:
    async with platform_session() as session:
        if args.key not in SETTINGS:
            _out(f"Unknown setting: {args.key}")
            return EXIT_ERROR
        await settings_service.unset(
            session, args.key, brand_id=args.brand, changed_by=args.actor or "cli"
        )
        _out(f"Unset {args.key}; it now falls back to the default.")
    return EXIT_OK


# --------------------------------------------------------------------- brands


async def cmd_brand_add(args: argparse.Namespace) -> int:
    """Create a brand, its encryption key and its table partitions.

    The partitions are created in the same transaction as the brand row: a
    brand without partitions has nowhere to store CDRs or recordings, and
    discovering that later means a failed insert in a worker rather than a
    clear error here.
    """
    async with platform_session() as session:
        existing = (
            await session.execute(select(Brand).where(Brand.slug == args.slug))
        ).scalar_one_or_none()
        if existing is not None:
            _out(f"Brand {args.slug!r} already exists (id {existing.id}).")
            return EXIT_ERROR

        key_id, wrapped = generate_data_key()
        brand = Brand(
            name=args.name, slug=args.slug, encryption_key_id=key_id, encryption_key_wrapped=wrapped
        )
        session.add(brand)
        await session.flush()
        await _scope_to_brand(session, brand.id)

        for table in ("cdrs", "recordings"):
            await session.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {table}_brand_{brand.id} "
                    f"PARTITION OF {table} FOR VALUES IN ({brand.id})"
                )
            )
        await session.execute(
            text(
                "INSERT INTO retention_policies (brand_id) VALUES (:b) "
                "ON CONFLICT (brand_id) DO NOTHING"
            ),
            {"b": brand.id},
        )
        _out(f"Created brand {brand.name!r} (id {brand.id}, slug {brand.slug}).")
        _out(f"  partitions: cdrs_brand_{brand.id}, recordings_brand_{brand.id}")
        _out(f"  encryption key id: {key_id[:12]}...")
    return EXIT_OK


async def cmd_brand_list(_args: argparse.Namespace) -> int:
    async with platform_session() as session:
        rows = (await session.execute(select(Brand).order_by(Brand.id))).scalars().all()
        if not rows:
            _out("No brands yet. Create one with: c2w-admin brand add --name X --slug x")
            return EXIT_OK
        _out(f"{'ID':>4}  {'NAME':28} {'SLUG':20} ACTIVE  KEY")
        for b in rows:
            key = (b.encryption_key_id or "-")[:12]
            _out(f"{b.id:>4}  {b.name:28} {b.slug:20} {'yes' if b.is_active else 'no':6}  {key}")
    return EXIT_OK


async def cmd_tenant_add(args: argparse.Namespace) -> int:
    async with platform_session() as session:
        brand = (
            await session.execute(select(Brand).where(Brand.slug == args.brand))
        ).scalar_one_or_none()
        if brand is None:
            _out(f"No such brand: {args.brand}")
            return EXIT_ERROR
        await _scope_to_brand(session, brand.id)
        tenant = Tenant(
            brand_id=brand.id, name=args.name, slug=args.slug, commpeak_domain=args.domain
        )
        session.add(tenant)
        await session.flush()
        _out(f"Created tenant {tenant.name!r} (id {tenant.id}) under brand {brand.slug}.")
    return EXIT_OK


# ---------------------------------------------------------------- connections


async def cmd_connection_add(args: argparse.Namespace) -> int:
    """Register a CommPeak S3 account.

    Credentials are prompted for and sealed immediately.  Nothing is read from
    the source at this point -- use ``connection test`` to probe, which is a
    read-only operation.
    """
    async with platform_session() as session:
        tenant = (
            await session.execute(
                select(Tenant)
                .join(Brand)
                .where(Tenant.slug == args.tenant, Brand.slug == args.brand)
            )
        ).scalar_one_or_none()
        if tenant is None:
            _out(f"No tenant {args.tenant!r} under brand {args.brand!r}.")
            return EXIT_ERROR
        brand = (
            await session.execute(select(Brand).where(Brand.id == tenant.brand_id))
        ).scalar_one()
        if not brand.encryption_key_id:
            _out(f"Brand {brand.slug} has no encryption key; recreate it with 'brand add'.")
            return EXIT_ERROR

        token = args.token or getpass.getpass("CommPeak S3 token: ")
        secret = args.secret or getpass.getpass("CommPeak S3 secret: ")
        if not token or not secret:
            _out("Both a token and a secret are required.")
            return EXIT_ERROR

        await _scope_to_brand(session, brand.id)
        conn = CommPeakConnection(
            brand_id=brand.id,
            tenant_id=tenant.id,
            name=args.name,
            s3_endpoint=args.endpoint,
            s3_bucket=args.bucket,
            s3_access_key_sealed="",
            s3_secret_sealed="",
        )
        session.add(conn)
        await session.flush()
        # AAD binds each ciphertext to this connection and field, so a sealed
        # value cannot be moved to another row and still decrypt.
        conn.s3_access_key_sealed = seal(
            token,
            key_id=brand.encryption_key_id,
            wrapped_key=brand.encryption_key_wrapped or "",
            aad=f"connection:{conn.id}:access_key",
        )
        conn.s3_secret_sealed = seal(
            secret,
            key_id=brand.encryption_key_id,
            wrapped_key=brand.encryption_key_wrapped or "",
            aad=f"connection:{conn.id}:secret_key",
        )
        _out(f"Created connection {conn.name!r} (id {conn.id}) for bucket {conn.s3_bucket}.")
        _out(f"Test it read-only with: c2w-admin connection test --id {conn.id}")
    return EXIT_OK


async def cmd_connection_list(_args: argparse.Namespace) -> int:
    async with platform_session() as session:
        rows = []
        for brand_id in await _scope_all_brands(session):
            await _scope_to_brand(session, brand_id)
            stmt = select(CommPeakConnection).order_by(CommPeakConnection.id)
            found = await session.execute(stmt)
            rows.extend(found.scalars().all())
        if not rows:
            _out("No CommPeak connections yet.")
            return EXIT_OK
        _out(f"{'ID':>4}  {'NAME':22} {'BUCKET':40} {'STATUS':9} LAST INVENTORY")
        for c in rows:
            last = (
                c.last_inventory_at.strftime("%Y-%m-%d %H:%M") if c.last_inventory_at else "never"
            )
            _out(f"{c.id:>4}  {c.name:22} {c.s3_bucket:40} {c.status:9} {last}")
    return EXIT_OK


async def cmd_destination_list(_args: argparse.Namespace) -> int:
    async with platform_session() as session:
        rows = []
        for brand_id in await _scope_all_brands(session):
            await _scope_to_brand(session, brand_id)
            stmt = select(StorageDestination).order_by(StorageDestination.id)
            found = await session.execute(stmt)
            rows.extend(found.scalars().all())
        if not rows:
            _out("No storage destinations configured yet.")
            _out("Transfers stay disabled until one exists; inventory and CDR search work now.")
            return EXIT_OK
        _out(f"{'ID':>4}  {'NAME':22} {'PROVIDER':10} {'REGION':16} {'BUCKET':28} STATUS")
        for d in rows:
            _out(f"{d.id:>4}  {d.name:22} {d.provider:10} {d.region:16} {d.bucket:28} {d.status}")
    return EXIT_OK


# ----------------------------------------------------------------- diagnostics


async def cmd_doctor(_args: argparse.Namespace) -> int:
    """Check the things that stop this platform working, in dependency order."""
    boot = get_bootstrap()
    ok = True
    _out(f"c2w {__version__}")
    _out("")
    for label, value in boot.masked().items():
        _out(f"  {label:24} {value}")
    if not boot.master_key.get_secret_value():
        _out("\n  master key is MISSING: stored credentials cannot be unsealed.")
        ok = False

    try:
        async with platform_session() as session:
            version = (await session.execute(text("SELECT version()"))).scalar_one()
            _out(f"\n  database                 connected ({version.split(',')[0]})")

            revision = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            _out(f"  schema revision          {revision or 'NOT MIGRATED'}")
            if revision is None:
                ok = False

            trgm = (
                await session.execute(
                    text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")
                )
            ).scalar_one()
            _out(f"  pg_trgm                  {'present' if trgm else 'MISSING (CDR search)'}")

            brands = (await session.execute(text("SELECT count(*) FROM brands"))).scalar_one()
            users = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()

            # Connections and destinations are brand-scoped, so counting them
            # without a scope returns zero under RLS -- which reads as "nothing
            # configured" and sends an operator looking in the wrong place.
            # Count per brand instead of relying on BYPASSRLS.
            conns = dests = 0
            for brand_id in await _scope_all_brands(session):
                await _scope_to_brand(session, brand_id)
                conns += (
                    await session.execute(
                        text("SELECT count(*) FROM commpeak_connections WHERE brand_id = :b"),
                        {"b": brand_id},
                    )
                ).scalar_one()
                dests += (
                    await session.execute(
                        text("SELECT count(*) FROM storage_destinations WHERE brand_id = :b"),
                        {"b": brand_id},
                    )
                ).scalar_one()
            _out(f"  brands / users           {brands} / {users}")
            _out(f"  connections / archives   {conns} / {dests}")

            read_only = await settings_service.get_bool(session, "source.read_only")
            transfers = await settings_service.get_bool(session, "transfer.enabled")
            _out(f"  CommPeak read-only       {'yes' if read_only else 'NO -- writes permitted'}")
            _out(f"  transfers enabled        {'yes' if transfers else 'no'}")

            if users == 0:
                _out("\n  Next: c2w-admin superadmin create --email you@example.com")
            elif brands == 0:
                _out("\n  Next: c2w-admin brand add --name 'Go4Rex' --slug go4rex")
            elif dests == 0:
                _out("\n  Next: add a storage destination, then set transfer.enabled true")
    except Exception as exc:
        _out(f"\n  database                 FAILED: {type(exc).__name__}: {exc}")
        ok = False

    return EXIT_OK if ok else EXIT_ERROR


# ------------------------------------------------------------------ arg parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="c2w-admin",
        description="Administer the c2w recording platform. All configuration is stored "
        "in the database; there are no config files to edit.",
    )
    parser.add_argument("--version", action="version", version=f"c2w {__version__}")
    sub = parser.add_subparsers(dest="group", required=True)

    # superadmin
    sa = sub.add_parser("superadmin", help="manage the local super admin").add_subparsers(
        dest="cmd", required=True
    )
    p = sa.add_parser("create", help="create the local super admin")
    p.add_argument("--email", required=True)
    p.add_argument("--name")
    p.add_argument("--password", help="omit to be prompted (preferred: keeps it out of history)")
    p.set_defaults(func=cmd_superadmin_create)

    p = sa.add_parser("passwd", help="reset a password and revoke sessions")
    p.add_argument("--email", required=True)
    p.add_argument("--password")
    p.set_defaults(func=cmd_superadmin_passwd)

    # user
    u = sub.add_parser("user", help="inspect users").add_subparsers(dest="cmd", required=True)
    u.add_parser("list", help="list users").set_defaults(func=cmd_user_list)

    # settings
    st = sub.add_parser("settings", help="read and write configuration").add_subparsers(
        dest="cmd", required=True
    )
    p = st.add_parser("list", help="show effective settings")
    p.add_argument("--brand", type=int, help="show a brand's effective values")
    p.add_argument("--category")
    p.add_argument("-v", "--verbose", action="store_true", help="include descriptions")
    p.set_defaults(func=cmd_settings_list)

    p = st.add_parser("get", help="read one setting")
    p.add_argument("key")
    p.add_argument("--brand", type=int)
    p.set_defaults(func=cmd_settings_get)

    p = st.add_parser("set", help="write one setting")
    p.add_argument("key")
    p.add_argument("value", nargs="?", help="omit for secrets to be prompted without echo")
    p.add_argument("--brand", type=int, help="set as a brand override")
    p.add_argument("--actor", help="who is making this change (recorded in history)")
    p.set_defaults(func=cmd_settings_set)

    p = st.add_parser(
        "reveal", help="print a shell-exportable setting in clear text"
    )
    p.add_argument("key")
    p.add_argument("--brand", type=int)
    p.set_defaults(func=cmd_settings_reveal)

    p = st.add_parser("unset", help="remove an override")
    p.add_argument("key")
    p.add_argument("--brand", type=int)
    p.add_argument("--actor")
    p.set_defaults(func=cmd_settings_unset)

    # brand / tenant
    br = sub.add_parser("brand", help="manage brands").add_subparsers(dest="cmd", required=True)
    p = br.add_parser("add", help="create a brand with keys and partitions")
    p.add_argument("--name", required=True)
    p.add_argument("--slug", required=True)
    p.set_defaults(func=cmd_brand_add)
    br.add_parser("list", help="list brands").set_defaults(func=cmd_brand_list)

    te = sub.add_parser("tenant", help="manage tenants").add_subparsers(dest="cmd", required=True)
    p = te.add_parser("add", help="create a tenant under a brand")
    p.add_argument("--brand", required=True, help="brand slug")
    p.add_argument("--name", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--domain", help="e.g. go4rex.pbx.commpeak.com")
    p.set_defaults(func=cmd_tenant_add)

    # connections / destinations
    cn = sub.add_parser("connection", help="manage CommPeak connections").add_subparsers(
        dest="cmd", required=True
    )
    p = cn.add_parser("add", help="register a CommPeak S3 account")
    p.add_argument("--brand", required=True)
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--bucket", required=True, help="the CommPeak account UUID")
    p.add_argument("--endpoint", default=COMMPEAK_ENDPOINT)
    p.add_argument("--token", help="omit to be prompted")
    p.add_argument("--secret", help="omit to be prompted")
    p.set_defaults(func=cmd_connection_add)
    cn.add_parser("list", help="list connections").set_defaults(func=cmd_connection_list)

    de = sub.add_parser("destination", help="manage archive destinations").add_subparsers(
        dest="cmd", required=True
    )
    de.add_parser("list", help="list destinations").set_defaults(func=cmd_destination_list)

    # doctor
    sub.add_parser("doctor", help="check configuration and connectivity").set_defaults(
        func=cmd_doctor
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging("c2w-admin", level_name="WARNING", json_output=False)
    args = build_parser().parse_args(argv)

    async def run() -> int:
        try:
            return await args.func(args)
        finally:
            await dispose_engine()

    # An operator running a command wants to be told what went wrong, not handed
    # a SQLAlchemy traceback. Full detail still goes to the log at DEBUG.
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        _out("\nCancelled.")
        return EXIT_ERROR
    except IntegrityError as exc:
        _out(f"Rejected by the database: {_pg_detail(exc)}")
        return EXIT_ERROR
    except OperationalError as exc:
        _out(f"Cannot reach the database: {_pg_detail(exc)}")
        _out("Check C2W_DATABASE_URL in the systemd unit, and that PostgreSQL is running.")
        return EXIT_ERROR
    except SQLAlchemyError as exc:
        _out(f"Database error: {_pg_detail(exc)}")
        return EXIT_ERROR
    except CryptoError as exc:
        _out(f"Credential error: {exc}")
        return EXIT_ERROR


def _pg_detail(exc: BaseException) -> str:
    """Pull the useful line out of a driver exception.

    PostgreSQL's own message and DETAIL are the parts that tell an operator what
    to change; the wrapping and the SQL echo are noise.
    """
    original = getattr(exc, "orig", None) or exc
    text_ = str(original)
    parts = [
        line.strip()
        for line in text_.splitlines()
        if line.strip() and not line.strip().startswith(("[SQL:", "[parameters:", "(Background"))
    ]
    message = " ".join(parts) if parts else type(exc).__name__
    # Strip the driver's class-name prefix, which says nothing useful.
    if ">: " in message:
        message = message.split(">: ", 1)[1]
    return message[:400]


if __name__ == "__main__":
    sys.exit(main())
