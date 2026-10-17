"""Password hashing for local accounts (stdlib scrypt, per-user salt)."""
import base64
import hashlib
import hmac
import secrets

N, R, P = 2**14, 8, 1


def _scrypt(password, salt, n=N, r=R, p=P):
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(password):
    salt = secrets.token_bytes(16)
    encode = lambda b: base64.b64encode(b).decode()
    return f'scrypt${N}${R}${P}${encode(salt)}${encode(_scrypt(password, salt))}'


# Verified against when the account does not exist, so lookups cost the same either way.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt, expected = (stored or DUMMY_HASH).split('$')
        if scheme != 'scrypt': return False
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(actual, base64.b64decode(expected)) and stored is not None
    except (ValueError, TypeError):
        return False
