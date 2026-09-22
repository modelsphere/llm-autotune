"""Minting and checking service credentials.

Kept apart from `core.auth` (which is about people, sessions and passwords)
because the threat model is different: a key has no expiry to lean on, cannot
be rotated by its holder, and is likely to end up in someone's deploy config.
That makes "shown once, stored hashed, revocable, attributable" the whole
design.
"""

import hashlib
import secrets

# Recognisable in a config file or a log at a glance, and greppable in a repo
# by anyone auditing for a leaked credential.
KEY_PREFIX = "atk"
_PREFIX_CHARS = 8
_SECRET_BYTES = 32


def mint() -> tuple[str, str, str]:
    """A new key: (plaintext, lookup prefix, hash).

    Plaintext is `atk_<prefix>_<secret>`. The prefix is carried in the key
    itself so verification is one indexed lookup — without it, checking a key
    would mean hashing it against every row in the table.
    """
    prefix = secrets.token_hex(_PREFIX_CHARS // 2)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    plaintext = f"{KEY_PREFIX}_{prefix}_{secret}"
    return plaintext, prefix, digest(plaintext)


def digest(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def split(plaintext: str | None) -> str | None:
    """The lookup prefix inside a presented key, or None if it is malformed.

    Cheap structural rejection before touching the database: a garbage header
    on a scanning bot's request should not cost a query.
    """
    if not plaintext:
        return None
    parts = plaintext.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX or not parts[1]:
        return None
    return parts[1]


def matches(plaintext: str, key_hash: str) -> bool:
    """Constant-time compare, so a wrong key cannot be narrowed down by timing."""
    return secrets.compare_digest(digest(plaintext), key_hash)


def masked(prefix: str) -> str:
    """What a key looks like once it can no longer be shown: `atk_1a2b3c4d…`."""
    return f"{KEY_PREFIX}_{prefix}…"
