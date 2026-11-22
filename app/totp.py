"""Time-based one-time codes (RFC 6238), the 6-digit codes authenticator apps show.

Standard settings every app understands: SHA-1, 6 digits, 30-second steps.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1  # also accept the code from one step before/after, for clock drift


def new_secret() -> str:
    """160 random bits, base32 without padding (what apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP)


def verify(secret: str, code: str, *, last_step: int | None = None, now: float | None = None) -> int | None:
    """Return the step the code matched, or None. A step at or before `last_step` is refused,
    so a code that was already used (or seen over someone's shoulder) can't be replayed."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    now_step = current_step(now)
    for step in range(now_step - WINDOW, now_step + WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def uri(secret: str, account: str, issuer: str = "Banktab") -> str:
    """otpauth:// link that authenticator apps read from the QR code."""
    label = quote(f"{issuer}:{account}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def qr_svg_data_uri(text: str) -> str:
    """The link as a QR code image (an SVG data: URI, allowed by the page's content policy)."""
    import segno
    return segno.make(text, error="m").svg_data_uri(scale=6, border=2, dark="#000", light="#fff")


# ---------------------------------------------------------------- recovery codes

RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i look-alikes


def new_recovery_codes(n: int = 10) -> list[str]:
    """Like 'k7mq-3xht-9pwd': 12 characters, about 59 bits each."""
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(12))
        out.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return out


def normalize_recovery(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())
