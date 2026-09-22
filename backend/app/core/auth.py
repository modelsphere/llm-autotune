from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import apikeys
from app.core.config import get_settings
from app.db.base import get_async_session
from app.db.models import ApiKey, Policy, User, UserRole
from app.db.models import PolicySession as PolicySessionModel

_bearer = HTTPBearer(auto_error=False)
# auto_error=False so a request carrying only a JWT is not rejected here before
# the bearer path gets a chance to run.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _expired(expires_at: datetime | None) -> bool:
    """Whether a key's expiry has passed. Naive timestamps (sqlite strips the
    zone) are read as UTC — the only zone the platform writes."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_token(user: User) -> str:
    settings = get_settings()
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "exp": datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_async_session),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    user = (
        await session.execute(select(User).where(User.id == int(payload["sub"])))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


# -- machine-to-machine -------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Whoever is making this request, person or service.

    Endpoints that both a browser and an external system may call take this
    instead of `User`, so neither has to care which the other is. `actor` is
    what lands in the audit trail: `alice` or `key:fleet-manager`, never the
    credential itself.
    """

    user: User
    actor: str
    via_key: bool = False


async def _principal_from_key(
    presented: str, session: AsyncSession
) -> Principal | None:
    prefix = apikeys.split(presented)
    if prefix is None:
        return None
    key = (
        await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
    ).scalar_one_or_none()
    if key is None or not key.active or not apikeys.matches(presented, key.key_hash):
        return None
    # A policy-session token is scoped to its session and nothing else: it must
    # not double as a general service key, however valid its hash is. The
    # policy router resolves it with get_policy_session instead.
    if key.policy_session_id is not None:
        return None
    if _expired(key.expires_at):
        return None
    owner = await session.get(User, key.owner_id)
    if owner is None:
        return None
    # Cheap liveness signal, and the only way to tell a key that is still in
    # use from one that was minted and forgotten — which is what you need to
    # know before revoking anything.
    key.last_used_at = datetime.now(UTC)
    await session.commit()
    return Principal(user=owner, actor=f"key:{key.name}", via_key=True)


async def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    api_key: str | None = Depends(_api_key_header),
    session: AsyncSession = Depends(get_async_session),
) -> Principal:
    """Accept either a browser session or a service key.

    The key is tried first: an external caller sends only that header, and
    falling through to the JWT path would answer it with "Not authenticated"
    rather than something it can act on.
    """
    if api_key:
        principal = await _principal_from_key(api_key, session)
        if principal is not None:
            return principal
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API key")
    user = await get_current_user(credentials, session)
    return Principal(user=user, actor=user.username)


async def require_admin_principal(
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Admin-only for endpoints a service key may also call: the key's OWNER
    must be an admin. `require_admin` above understands browser sessions only,
    which would lock an admin's own automation out of admin work."""
    if principal.user.role != UserRole.ADMIN.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return principal


# -- policy sessions -----------------------------------------------------------


@dataclass(frozen=True)
class PolicyCaller:
    """The policy container behind a session token.

    Deliberately not a Principal: a session token identifies a *night*, not a
    person or a fleet, and every route that takes this is implicitly scoped to
    `session` — handlers never see a campaign id from the wire without checking
    it against this row.
    """

    session: "PolicySessionModel"
    actor: str  # "policy:<name>" in the audit trail


async def get_policy_session(
    api_key: str | None = Depends(_api_key_header),
    session: AsyncSession = Depends(get_async_session),
) -> PolicyCaller:
    """Resolve a policy-session token, and only such a token.

    401 for anything unpresentable (missing, wrong, revoked, expired, or a
    perfectly good key that is not session-scoped), 410 once the session is
    terminal — the distinction matters to the caller: 401 means "you are
    nobody", 410 means "you were somebody and the night is over, exit 0".
    """
    if not api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Policy session token required")
    prefix = apikeys.split(api_key)
    key = (
        (await session.execute(select(ApiKey).where(ApiKey.prefix == prefix)))
        .scalar_one_or_none()
        if prefix is not None
        else None
    )
    if (
        key is None
        or not key.active
        or not apikeys.matches(api_key, key.key_hash)
        or key.policy_session_id is None
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid policy session token")
    if _expired(key.expires_at):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Policy session token expired")
    policy_session = await session.get(PolicySessionModel, key.policy_session_id)
    if policy_session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid policy session token")
    if policy_session.terminal:
        raise HTTPException(status.HTTP_410_GONE, "Policy session has ended")
    key.last_used_at = datetime.now(UTC)
    await session.commit()
    policy = await session.get(Policy, policy_session.policy_id)
    name = policy.name if policy is not None else str(policy_session.policy_id)
    return PolicyCaller(session=policy_session, actor=f"policy:{name}")
