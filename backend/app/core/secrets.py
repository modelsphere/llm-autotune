"""Reversible encryption for the credentials users entrust to the platform.

Only one kind so far: a cluster's kubeconfig. It cannot be hashed like a
password because it has to be replayed to the cluster on every call —
hours after it was typed, with nobody around to retype it — so it is encrypted
at rest instead, keyed off the deployment's JWT secret.

That key choice has a consequence worth stating: rotating `jwt_secret` makes
every stored token undecryptable. Nothing breaks (a token that will not decrypt
is treated as absent, so an entry is simply not forwarded) but users have to
re-enter them. There is no key rotation here because there is nothing yet whose
loss is worse than re-typing a token.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a credential for storage. Empty in, empty out — an absent
    secret is stored as "" rather than as the encryption of nothing."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(stored: str) -> str:
    """The plaintext, or "" when there is none — including when the stored
    value cannot be decrypted (a rotated key, a hand-edited row). Callers treat
    "" as "no credential", which is always the safe reading: it makes the
    platform do less, never more."""
    if not stored:
        return ""
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except (InvalidToken, ValueError):
        logger.warning("a stored secret could not be decrypted; treating it as absent")
        return ""
