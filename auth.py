"""Password hashing, session tokens, and at-rest encryption for per-user
SMTP credentials. Uses only the standard library (hashlib/hmac/secrets) so
no extra native dependencies are needed on Railway.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from config import SECRET_KEY

_PBKDF2_ITERATIONS = 260_000
_SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # 30 days


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    import os as _os

    salt = _os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        salt_hex, digest_hex = hashed.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# Signed session tokens (HMAC-SHA256), stored in an HttpOnly cookie
# ---------------------------------------------------------------------------
def _sign(payload: bytes) -> str:
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


def create_session_token(user_id: int) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + _SESSION_MAX_AGE_SECONDS}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_sign(payload)}"


def verify_session_token(token: str) -> Optional[int]:
    try:
        payload_b64, sig = token.split(".", 1)
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return int(data["uid"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Symmetric encryption for SMTP app passwords at rest (AES via XOR-stream is
# NOT used here; we derive a keystream with HMAC-SHA256 in counter mode,
# which gives strong stream-cipher security using only the standard library).
# ---------------------------------------------------------------------------
def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    key = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    import os as _os

    nonce = _os.urandom(16)
    data = plaintext.encode("utf-8")
    ks = _keystream(key, nonce, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, ks))
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        key = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
        raw = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
        nonce, data = raw[:16], raw[16:]
        ks = _keystream(key, nonce, len(data))
        plaintext = bytes(a ^ b for a, b in zip(data, ks))
        return plaintext.decode("utf-8")
    except Exception:
        return ""
