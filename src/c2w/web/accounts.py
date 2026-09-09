"""Adding and testing CommPeak accounts and archive storage, from the browser.

These used to be command-line only, which meant the console could not be
configured without shell access to the server -- so the pages that listed them
told you to go and run a command instead, which is not a console.

Credentials still never travel further than they must: they arrive over the
session that is already authenticated, are sealed with the organisation's data
key before the row is written, and are never sent back to a browser. What a
page can show is whether a stored credential works, which is what the Test
button is for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.crypto import CryptoError, generate_data_key, seal
from c2w.db.base import ConnectionStatus
from c2w.db.models.core import Brand, CommPeakConnection, StorageDestination, Tenant
from c2w.logging import get_logger
from c2w.storage.commpeak import COMMPEAK_ENDPOINT, run_source_probes
from c2w.storage.errors import TransferError
from c2w.storage.factory import open_destination, open_source
from c2w.storage.wasabi import WASABI_REGIONS

log = get_logger(__name__)

__all__ = [
    "AccountError",
    "add_connection",
    "add_destination",
    "delete_connection",
    "delete_destination",
    "test_connection",
    "test_destination",
    "update_connection",
    "update_destination",
]


class AccountError(ValueError):
    """Something about the submitted account is wrong, and it is worth saying
    which -- an unhelpful "invalid input" on a form of fifteen fields is a
    guessing game."""


@dataclass(slots=True)
class ProbeOutcome:
    ok: bool
    summary: str
    checks: list[dict[str, Any]]


async def _brand_keys(session: AsyncSession, brand_id: int) -> tuple[Brand, str, str]:
    """The organisation and its data key, creating the key if it has none.

    An organisation created before envelope encryption existed has no key; the
    alternative to making one here is refusing to save a credential, with
    nothing the operator can do about it from the console.
    """
    brand = (
        await session.execute(select(Brand).where(Brand.id == brand_id))
    ).scalar_one_or_none()
    if brand is None:
        raise AccountError("that organisation no longer exists")
    if not brand.encryption_key_id or not brand.encryption_key_wrapped:
        key_id, wrapped = generate_data_key()
        brand.encryption_key_id, brand.encryption_key_wrapped = key_id, wrapped
        await session.flush()
        log.info("accounts.data_key_created", brand_id=brand.id)
    return brand, brand.encryption_key_id, brand.encryption_key_wrapped


def _require(value: str | None, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise AccountError(f"{field} is required")
    return text


async def _tenant_for_domain(
    session: AsyncSession, brand_id: int, domain: str
) -> Tenant:
    """Find or create the tenant for a CommPeak domain.

    A tenant is only ever the CommPeak domain a bucket belongs to, so asking
    for it separately would be asking the same question twice. The slug is
    derived rather than typed for the same reason.
    """
    domain = domain.strip().lower()
    existing = (
        await session.execute(
            select(Tenant).where(Tenant.brand_id == brand_id, Tenant.commpeak_domain == domain)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    slug = domain.split(".")[0][:80] or "tenant"
    taken = {
        s
        for s in (
            await session.execute(select(Tenant.slug).where(Tenant.brand_id == brand_id))
        )
        .scalars()
        .all()
    }
    candidate, n = slug, 1
    while candidate in taken:
        n += 1
        candidate = f"{slug}-{n}"

    tenant = Tenant(brand_id=brand_id, name=domain, slug=candidate, commpeak_domain=domain)
    session.add(tenant)
    await session.flush()
    return tenant


# ------------------------------------------------------------ CommPeak accounts


async def add_connection(
    session: AsyncSession, brand_id: int, form: dict[str, str], *, actor: str
) -> CommPeakConnection:
    """Create a CommPeak account for this organisation.

    An organisation has as many of these as it has PBXes and dialers; each one
    is a bucket plus the credentials that reach it.
    """
    brand, key_id, wrapped = await _brand_keys(session, brand_id)
    name = _require(form.get("name"), "A name")
    domain = _require(form.get("commpeak_domain"), "The CommPeak domain")
    bucket = _require(form.get("s3_bucket"), "The bucket")
    token = _require(form.get("s3_access_key"), "The S3 token")
    secret = _require(form.get("s3_secret"), "The S3 secret")

    duplicate = (
        await session.execute(
            select(CommPeakConnection.id).where(
                CommPeakConnection.brand_id == brand_id,
                CommPeakConnection.s3_bucket == bucket,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise AccountError(f"bucket {bucket} is already registered for {brand.name}")

    tenant = await _tenant_for_domain(session, brand_id, domain)

    conn = CommPeakConnection(
        brand_id=brand_id,
        tenant_id=tenant.id,
        name=name,
        s3_endpoint=(form.get("s3_endpoint") or COMMPEAK_ENDPOINT).strip(),
        s3_region=(form.get("s3_region") or "us-east-1").strip(),
        s3_bucket=bucket,
        s3_access_key_sealed="",
        s3_secret_sealed="",
        cdr_api_base=(form.get("cdr_api_base") or "").strip() or None,
        cdr_api_user=(form.get("cdr_api_user") or "").strip() or None,
        destination_id=int(form["destination_id"]) if form.get("destination_id") else None,
        status=ConnectionStatus.UNTESTED,
    )
    session.add(conn)
    await session.flush()

    # Sealed after the flush so the row id can bind each ciphertext to this
    # connection and this field -- a sealed value cannot then be moved to
    # another row and still open.
    conn.s3_access_key_sealed = seal(
        token, key_id=key_id, wrapped_key=wrapped, aad=f"connection:{conn.id}:access_key"
    )
    conn.s3_secret_sealed = seal(
        secret, key_id=key_id, wrapped_key=wrapped, aad=f"connection:{conn.id}:secret_key"
    )
    if cdr_token := (form.get("cdr_api_token") or "").strip():
        conn.cdr_api_key_sealed = seal(
            cdr_token, key_id=key_id, wrapped_key=wrapped, aad=f"connection:{conn.id}:cdr_api_key"
        )
    await session.flush()
    log.info("accounts.connection_added", connection_id=conn.id, actor=actor, bucket=bucket)
    return conn


async def update_connection(
    session: AsyncSession, brand_id: int, connection_id: int, form: dict[str, str], *, actor: str
) -> CommPeakConnection:
    """Change an account. Blank credential fields keep what is stored."""
    _, key_id, wrapped = await _brand_keys(session, brand_id)
    conn = (
        await session.execute(
            select(CommPeakConnection).where(
                CommPeakConnection.id == connection_id,
                CommPeakConnection.brand_id == brand_id,
            )
        )
    ).scalar_one_or_none()
    if conn is None:
        raise AccountError("that CommPeak account no longer exists")

    if name := (form.get("name") or "").strip():
        conn.name = name
    if domain := (form.get("commpeak_domain") or "").strip():
        conn.tenant_id = (await _tenant_for_domain(session, brand_id, domain)).id
    if endpoint := (form.get("s3_endpoint") or "").strip():
        conn.s3_endpoint = endpoint
    conn.cdr_api_base = (form.get("cdr_api_base") or "").strip() or None
    conn.cdr_api_user = (form.get("cdr_api_user") or "").strip() or None
    conn.destination_id = int(form["destination_id"]) if form.get("destination_id") else None
    conn.is_enabled = form.get("is_enabled") == "true"

    # An empty box means "keep it": the box is always empty on load, so
    # treating blank as a clear would wipe the credential on every save.
    for field, aad in (
        ("s3_access_key", "access_key"),
        ("s3_secret", "secret_key"),
        ("cdr_api_token", "cdr_api_key"),
    ):
        if value := (form.get(field) or "").strip():
            sealed = seal(
                value, key_id=key_id, wrapped_key=wrapped, aad=f"connection:{conn.id}:{aad}"
            )
            if field == "s3_access_key":
                conn.s3_access_key_sealed = sealed
            elif field == "s3_secret":
                conn.s3_secret_sealed = sealed
            else:
                conn.cdr_api_key_sealed = sealed
            conn.status = ConnectionStatus.UNTESTED

    await session.flush()
    log.info("accounts.connection_updated", connection_id=conn.id, actor=actor)
    return conn


async def delete_connection(
    session: AsyncSession, brand_id: int, connection_id: int, *, actor: str
) -> str:
    """Forget an account.

    Its recordings and call records are left alone: they describe calls that
    happened, and the archive copies are still there and still playable. Only
    the way in is removed.
    """
    conn = (
        await session.execute(
            select(CommPeakConnection).where(
                CommPeakConnection.id == connection_id,
                CommPeakConnection.brand_id == brand_id,
            )
        )
    ).scalar_one_or_none()
    if conn is None:
        raise AccountError("that CommPeak account no longer exists")
    name = conn.name
    conn.is_enabled = False
    conn.status = ConnectionStatus.DISABLED
    conn.status_detail = f"removed by {actor}"
    await session.flush()
    log.info("accounts.connection_disabled", connection_id=connection_id, actor=actor)
    return name


async def test_connection(
    session: AsyncSession, brand_id: int, connection_id: int
) -> ProbeOutcome:
    """Read-only probe of a stored account.

    Each check is reported separately because distinguishing "wrong secret"
    from "this server's address is not on the account's access list" is most of
    the work of getting a new account going.
    """
    conn = (
        await session.execute(
            select(CommPeakConnection).where(
                CommPeakConnection.id == connection_id,
                CommPeakConnection.brand_id == brand_id,
            )
        )
    ).scalar_one_or_none()
    if conn is None:
        raise AccountError("that CommPeak account no longer exists")

    try:
        client = await open_source(session, conn)
    except (TransferError, CryptoError) as exc:
        conn.status = ConnectionStatus.ERROR
        conn.status_detail = str(exc)[:2000]
        conn.last_probe_at = datetime.now(UTC)
        await session.flush()
        return ProbeOutcome(False, str(exc), [])

    async with client as source:
        results = await run_source_probes(source)

    checks = [
        {
            "name": r.name.replace("_", " "),
            "ok": r.ok,
            "detail": r.detail,
            "hint": r.hint or "",
        }
        for r in results
    ]
    ok = all(r.ok for r in results)
    conn.status = ConnectionStatus.OK if ok else ConnectionStatus.ERROR
    first_bad = next((r for r in results if not r.ok), None)
    conn.status_detail = None if ok else f"{first_bad.detail} — {first_bad.hint or ''}"[:2000]
    conn.last_probe_at = datetime.now(UTC)
    await session.flush()
    return ProbeOutcome(
        ok,
        "Reachable" if ok else (first_bad.detail if first_bad else "failed"),
        checks,
    )


# ------------------------------------------------------------- archive storage


async def add_destination(
    session: AsyncSession, brand_id: int, form: dict[str, str], *, actor: str
) -> StorageDestination:
    """Add somewhere to keep verified copies.

    Modelled as a generic S3 service rather than "a Wasabi account", so an
    organisation can be moved to another provider without touching the engine.
    """
    brand, key_id, wrapped = await _brand_keys(session, brand_id)
    name = _require(form.get("name"), "A name")
    bucket = _require(form.get("bucket"), "The bucket")
    access_key = _require(form.get("access_key"), "The access key")
    secret_key = _require(form.get("secret_key"), "The secret key")
    region = (form.get("region") or "eu-central-1").strip()
    provider = (form.get("provider") or "wasabi").strip()

    endpoint = (form.get("endpoint") or "").strip()
    if not endpoint:
        if provider == "wasabi":
            endpoint = WASABI_REGIONS.get(region, "")
        if not endpoint:
            raise AccountError(
                f"no address is known for {provider} in {region}; enter one explicitly"
            )

    duplicate = (
        await session.execute(
            select(StorageDestination.id).where(
                StorageDestination.brand_id == brand_id, StorageDestination.name == name
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise AccountError(f"{brand.name} already has storage called {name!r}")

    dest = StorageDestination(
        brand_id=brand_id,
        name=name,
        provider=provider,
        endpoint=endpoint,
        region=region,
        bucket=bucket,
        path_prefix=(form.get("path_prefix") or "archive").strip(),
        access_key_sealed="",
        secret_sealed="",
        status=ConnectionStatus.UNTESTED,
    )
    session.add(dest)
    await session.flush()
    dest.access_key_sealed = seal(
        access_key, key_id=key_id, wrapped_key=wrapped, aad=f"destination:{dest.id}:access_key"
    )
    dest.secret_sealed = seal(
        secret_key, key_id=key_id, wrapped_key=wrapped, aad=f"destination:{dest.id}:secret_key"
    )
    await session.flush()
    log.info("accounts.destination_added", destination_id=dest.id, actor=actor, bucket=bucket)
    return dest


async def update_destination(
    session: AsyncSession, brand_id: int, destination_id: int, form: dict[str, str], *, actor: str
) -> StorageDestination:
    _, key_id, wrapped = await _brand_keys(session, brand_id)
    dest = (
        await session.execute(
            select(StorageDestination).where(
                StorageDestination.id == destination_id,
                StorageDestination.brand_id == brand_id,
            )
        )
    ).scalar_one_or_none()
    if dest is None:
        raise AccountError("that storage no longer exists")

    if name := (form.get("name") or "").strip():
        dest.name = name
    if region := (form.get("region") or "").strip():
        dest.region = region
        if dest.provider == "wasabi" and not (form.get("endpoint") or "").strip():
            dest.endpoint = WASABI_REGIONS.get(region, dest.endpoint)
    if endpoint := (form.get("endpoint") or "").strip():
        dest.endpoint = endpoint
    if bucket := (form.get("bucket") or "").strip():
        dest.bucket = bucket
    dest.path_prefix = (form.get("path_prefix") or "").strip()
    dest.is_enabled = form.get("is_enabled") == "true"

    for field, attr, aad in (
        ("access_key", "access_key_sealed", "access_key"),
        ("secret_key", "secret_sealed", "secret_key"),
    ):
        if value := (form.get(field) or "").strip():
            setattr(
                dest,
                attr,
                seal(
                    value, key_id=key_id, wrapped_key=wrapped, aad=f"destination:{dest.id}:{aad}"
                ),
            )
            dest.status = ConnectionStatus.UNTESTED

    await session.flush()
    log.info("accounts.destination_updated", destination_id=dest.id, actor=actor)
    return dest


async def delete_destination(
    session: AsyncSession, brand_id: int, destination_id: int, *, actor: str
) -> str:
    """Stop using a bucket.

    The bucket and everything in it are untouched -- this only stops new copies
    going there. Recordings already verified in it keep pointing at it, so they
    stay playable.
    """
    dest = (
        await session.execute(
            select(StorageDestination).where(
                StorageDestination.id == destination_id,
                StorageDestination.brand_id == brand_id,
            )
        )
    ).scalar_one_or_none()
    if dest is None:
        raise AccountError("that storage no longer exists")
    name = dest.name
    dest.is_enabled = False
    dest.status = ConnectionStatus.DISABLED
    dest.status_detail = f"stopped by {actor}"
    await session.flush()
    log.info("accounts.destination_disabled", destination_id=destination_id, actor=actor)
    return name


async def purge_destination(
    session: AsyncSession, brand_id: int, destination_id: int, *, actor: str
) -> str:
    """Delete a bucket's registration outright.

    "Stop using" is the safe action and stays the default, because a recording
    that has been verified into a bucket points at this row -- delete it and
    that recording has nowhere to be played from. But a bucket added by mistake
    has nothing pointing at it, and refusing to remove *that* is just a page
    that will not tidy up after itself.

    So: counted first, and refused with the count when anything depends on it.
    Nothing is touched in the bucket itself either way.
    """
    dest = (
        await session.execute(
            select(StorageDestination).where(
                StorageDestination.id == destination_id,
                StorageDestination.brand_id == brand_id,
            )
        )
    ).scalar_one_or_none()
    if dest is None:
        raise AccountError("that storage no longer exists")

    depends = (
        await session.execute(
            text(
                "SELECT count(*) FROM recordings "
                "WHERE brand_id = :b AND destination_id = :d"
            ),
            {"b": brand_id, "d": destination_id},
        )
    ).scalar_one()
    if depends:
        raise AccountError(
            f"{depends:,} recording(s) are archived in {dest.name} and point at it; "
            "removing it would leave them with nowhere to play from. Use "
            "\u201cstop using\u201d instead, which keeps them playable and sends no "
            "new copies there"
        )

    name = dest.name
    await session.execute(
        delete(StorageDestination).where(StorageDestination.id == destination_id)
    )
    await session.flush()
    log.info("accounts.destination_removed", destination_id=destination_id, actor=actor)
    return name


async def purge_connection(
    session: AsyncSession, brand_id: int, connection_id: int, *, actor: str
) -> str:
    """Delete a CommPeak account's registration outright.

    Same rule as a bucket, for the same reason: recordings and call records
    carry this connection's id, and they describe calls that really happened.
    An account registered by mistake has nothing pointing at it and can go.
    """
    conn = (
        await session.execute(
            select(CommPeakConnection).where(
                CommPeakConnection.id == connection_id,
                CommPeakConnection.brand_id == brand_id,
            )
        )
    ).scalar_one_or_none()
    if conn is None:
        raise AccountError("that CommPeak account no longer exists")

    counts = (
        await session.execute(
            text(
                "SELECT (SELECT count(*) FROM recordings "
                "         WHERE brand_id = :b AND connection_id = :c) AS recordings, "
                "       (SELECT count(*) FROM cdrs "
                "         WHERE brand_id = :b AND connection_id = :c) AS calls"
            ),
            {"b": brand_id, "c": connection_id},
        )
    ).mappings().one()
    if counts["recordings"] or counts["calls"]:
        raise AccountError(
            f"{counts['recordings']:,} recording(s) and {counts['calls']:,} call "
            f"record(s) came from {conn.name} and point at it. Use "
            "\u201cstop using\u201d instead, which leaves them intact and stops "
            "reading anything new"
        )

    name = conn.name
    await session.execute(
        delete(CommPeakConnection).where(CommPeakConnection.id == connection_id)
    )
    await session.flush()
    log.info("accounts.connection_removed", connection_id=connection_id, actor=actor)
    return name


async def test_destination(
    session: AsyncSession, brand_id: int, destination_id: int
) -> ProbeOutcome:
    """Prove the stored keys can list, write and read back."""
    dest = (
        await session.execute(
            select(StorageDestination).where(
                StorageDestination.id == destination_id,
                StorageDestination.brand_id == brand_id,
            )
        )
    ).scalar_one_or_none()
    if dest is None:
        raise AccountError("that storage no longer exists")

    checks: list[dict[str, Any]] = []

    def record(name: str, exc: BaseException | None, detail: str = "") -> bool:
        if exc is None:
            checks.append({"name": name, "ok": True, "detail": detail, "hint": ""})
            return True
        err = exc if isinstance(exc, TransferError) else None
        checks.append(
            {
                "name": name,
                "ok": False,
                "detail": err.message if err else str(exc),
                "hint": (err.hint if err else "") or "",
            }
        )
        return False

    try:
        client = await open_destination(session, dest)
    except (TransferError, CryptoError) as exc:
        record("credentials", exc)
        dest.status = ConnectionStatus.ERROR
        dest.status_detail = str(exc)[:2000]
        dest.last_probe_at = datetime.now(UTC)
        await session.flush()
        return ProbeOutcome(False, str(exc), checks)

    probe_key = f"{dest.path_prefix.strip('/') + '/' if dest.path_prefix else ''}.c2w-probe"
    async with client as archive:
        try:
            await archive.probe()
            record("reach the bucket", None, f"{dest.bucket} in {dest.region}")
        except BaseException as exc:
            record("reach the bucket", exc)

        # Writing and reading back is the only check that proves the keys can
        # actually archive: listing succeeds with read-only keys.
        try:
            await archive.put_bytes(probe_key, b"c2w probe", content_type="text/plain")
            record("write a test object", None, probe_key)
            meta = await archive.head(probe_key)
            record("read it back", None, f"{meta.size} bytes")
            await archive.delete(probe_key)
            record("tidy up after the test", None, "")
        except BaseException as exc:
            record("write a test object", exc)

    ok = all(c["ok"] for c in checks)
    dest.status = ConnectionStatus.OK if ok else ConnectionStatus.ERROR
    first_bad = next((c for c in checks if not c["ok"]), None)
    dest.status_detail = None if ok else f"{first_bad['detail']} — {first_bad['hint']}"[:2000]
    dest.last_probe_at = datetime.now(UTC)
    await session.flush()
    return ProbeOutcome(
        ok,
        "Reachable and writable" if ok else (first_bad["detail"] if first_bad else "failed"),
        checks,
    )


def wasabi_region_choices() -> list[str]:
    """Regions with a known address, for the menu."""
    return sorted(WASABI_REGIONS)
