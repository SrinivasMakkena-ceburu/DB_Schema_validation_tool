"""Encryption of stored database passwords.

The Fernet key lives in DATA_DIR/secret.key and is created on first use.
Losing the key means stored passwords must be re-entered.
"""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class DecryptError(Exception):
    pass


def _fernet():
    path = settings.DATA_DIR / "secret.key"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o600)
    return Fernet(path.read_bytes().strip())


def encrypt(plain):
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptError(
            "Stored password cannot be decrypted (secret.key changed?). Re-enter it."
        ) from exc
