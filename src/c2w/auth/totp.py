"""Authenticator-app second factor (TOTP, RFC 6238).

Written against the RFC with the standard library rather than pulled in as a
dependency: the algorithm is an HMAC, a counter and a truncation, and the
interesting parts of getting it right are not in the arithmetic.

Three of those parts are easy to leave out, and all three matter here, because
what this protects is a searchable archive of recorded customer phone calls:

* **A code must work only once.** The naive check accepts the same six digits
  for the whole 30-second step, so anyone who watches one being typed -- over a
  shoulder, over a screen share -- can reuse it. :func:`verify` refuses a
  counter that is not strictly newer than the last one accepted, which is why
  it returns the counter for the caller to persist.
* **Clocks drift.** A window of one step either side is checked, so a phone a
  few seconds out still works. Wider than that starts handing out a longer
  reuse window for no real gain.
* **Comparison is constant-time.** ``hmac.compare_digest``, not ``==``.

Recovery codes are hashed with the same Argon2 hasher as passwords, because
they *are* passwords -- a single-use one that bypasses the second factor. They
are shown once, at enrolment, and never again.
"""

from __future__ import annotations

import base64
import hmac
import io
import secrets
import struct
import time
from hashlib import sha1
from urllib.parse import quote

import segno

__all__ = [
    "DIGITS",
    "STEP_SECONDS",
    "WINDOW_STEPS",
    "code_at",
    "format_secret",
    "hash_recovery_code",
    "new_recovery_codes",
    "new_secret",
    "normalise_code",
    "provisioning_uri",
    "qr_svg",
    "verify",
    "verify_recovery_code",
]

#: 160 bits, the RFC's own recommendation and what every authenticator expects.
SECRET_BYTES = 20
DIGITS = 6
STEP_SECONDS = 30
#: One step either side: about a minute and a half of tolerable clock drift.
WINDOW_STEPS = 1
RECOVERY_CODES = 10


def new_secret() -> str:
    """A fresh base32 secret, unpadded, as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def format_secret(secret: str) -> str:
    """The secret in typeable groups of four.

    Not decoration: a QR code is unusable on a desktop authenticator, on a
    hardware token, or by anyone who cannot point a camera at their own screen,
    so the typed path has to be a real one.
    """
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


def _decode(secret: str) -> bytes:
    raw = secret.strip().replace(" ", "").upper()
    raw += "=" * (-len(raw) % 8)
    return base64.b32decode(raw, casefold=True)


def code_at(secret: str, counter: int) -> str:
    """The code for a given step counter."""
    digest = hmac.new(_decode(secret), struct.pack(">Q", counter), sha1).digest()
    offset = digest[-1] & 0x0F
    (truncated,) = struct.unpack(">I", digest[offset : offset + 4])
    return str((truncated & 0x7FFFFFFF) % (10**DIGITS)).zfill(DIGITS)


def normalise_code(entered: str) -> str:
    """Strip what people paste in: spaces, dashes, stray whitespace."""
    return "".join(ch for ch in str(entered or "") if ch.isdigit())


def verify(
    secret: str,
    entered: str,
    *,
    last_counter: int | None = None,
    at: float | None = None,
) -> int | None:
    """Check a code. Returns the counter it matched, or ``None``.

    The counter is returned rather than a bare ``True`` so the caller can store
    it and make the code single-use; passing it back as ``last_counter`` is
    what closes the replay window. A caller that ignores the return value has a
    working second factor that can be replayed for thirty seconds, which is
    exactly the mistake this signature is shaped to prevent.
    """
    digits = normalise_code(entered)
    if len(digits) != DIGITS:
        return None

    now = time.time() if at is None else at
    current = int(now // STEP_SECONDS)
    for drift in range(-WINDOW_STEPS, WINDOW_STEPS + 1):
        counter = current + drift
        if counter < 0:
            continue
        if last_counter is not None and counter <= last_counter:
            continue        # already used, or older than one already used
        if hmac.compare_digest(code_at(secret, counter), digits):
            return counter
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The ``otpauth://`` URI an authenticator scans or imports.

    The issuer is repeated in the label as well as the parameter -- older apps
    read only one of the two, and an entry that just says the account name is
    unidentifiable once somebody has three of them.
    """
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def qr_svg(uri: str, *, scale: int = 5) -> str:
    """An inline SVG QR code that follows the console's theme.

    SVG and inline on purpose: this console runs on a private network with no
    outbound access, so a third-party QR service would render an empty box --
    and the payload is a shared secret that has no business being sent to
    someone else's server to be drawn.

    segno will not accept ``currentColor`` as a colour, so the module is drawn
    in a sentinel and the sentinel is swapped afterwards. That leaves the fill
    inheriting the surrounding text colour, which is what makes it legible in
    the dark theme instead of a black square on a dark card.
    """
    sentinel = "#010203"
    out = io.BytesIO()   # segno's svg writer emits bytes, not text
    segno.make(uri, error="m").save(
        out, kind="svg", scale=scale, border=2,
        dark=sentinel, light=None, omitsize=True,
    )
    svg = out.getvalue().decode("utf-8").replace(sentinel, "currentColor")
    if sentinel in svg:  # pragma: no cover - defensive
        raise RuntimeError("QR sentinel colour was not replaced")
    # The XML prolog is invalid part-way through an HTML document.
    if svg.startswith("<?xml"):
        svg = svg[svg.index("?>") + 2 :].lstrip()
    return svg


def new_recovery_codes(count: int = RECOVERY_CODES) -> list[str]:
    """Single-use codes for when the phone is lost.

    Without these, losing a phone means an administrator has to reset the
    second factor -- and for the platform administrator there may be nobody
    above them to ask.
    """
    return [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        for _ in range(count)
    ]


def hash_recovery_code(code: str) -> str:
    from c2w.auth.local import hash_recovery_secret

    return hash_recovery_secret(_canonical_recovery(code))


def verify_recovery_code(hashes: list[str], entered: str) -> str | None:
    """Return the matching stored hash so the caller can remove it, or None.

    Removal is the caller's job because "single use" means the row changes, and
    a function that only said yes or no would make it easy to forget.
    """
    from c2w.auth.local import verify_password

    candidate = _canonical_recovery(entered)
    if not candidate:
        return None
    for stored in hashes:
        if verify_password(stored, candidate):
            return stored
    return None


def _canonical_recovery(code: str) -> str:
    """Case and separators should not decide whether a recovery code works."""
    return "".join(ch for ch in str(code or "").lower() if ch.isalnum())
