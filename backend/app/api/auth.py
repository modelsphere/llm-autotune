from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    create_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from app.db.base import get_async_session
from app.db.models import Event, User, UserRole
from app.schemas.core import (
    ContributorSet,
    LoginRequest,
    PasswordChange,
    RegisterRequest,
    TokenResponse,
    UserOut,
    UserRoleUpdate,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_async_session)):
    exists = (
        await session.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "username taken")
    # First user becomes admin — good enough for the PoC's own user system.
    user_count = (await session.execute(select(func.count(User.id)))).scalar_one()
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=UserRole.ADMIN.value if user_count == 0 else UserRole.USER.value,
    )
    session.add(user)
    await session.commit()
    return TokenResponse(access_token=create_token(user))


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_async_session)):
    user = (
        await session.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    return TokenResponse(access_token=create_token(user))


def _me_out(user: User) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return _me_out(user)


@router.put("/me/contributor", response_model=UserOut)
async def save_contributor(
    body: ContributorSet,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Save the name your submissions are published under.

    Typed once here instead of on every submission. Not a credential and not
    checked — a display name, carried through to the benchmark platform's own
    submission list.
    """
    user.contributor = body.contributor.strip()
    session.add(
        Event(actor=user.username, kind="contributor_saved",
              payload={"contributor": user.contributor})
    )
    await session.commit()
    return _me_out(user)


@router.delete("/me/contributor", response_model=UserOut)
async def clear_contributor(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Go back to being published under your username. Submissions already
    made keep the name they were made with."""
    user.contributor = ""
    session.add(Event(actor=user.username, kind="contributor_cleared", payload={}))
    await session.commit()
    return _me_out(user)


@router.post("/password", response_model=TokenResponse)
async def change_password(
    body: PasswordChange,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Change your own password.

    Returns a fresh token. The old one stays valid until it expires — these are
    stateless JWTs with no revocation list — so the honest thing is to hand back
    a new one rather than imply the old is dead. An API key is a separate
    credential and is unaffected; revoke those on the API keys page.
    """
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "current password is wrong")
    if body.new_password == body.current_password:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "the new password is the old one"
        )
    user.password_hash = hash_password(body.new_password)
    session.add(Event(actor=user.username, kind="password_changed", payload={}))
    await session.commit()
    return TokenResponse(access_token=create_token(user))


@router.get("/users", response_model=list[UserOut])
async def list_users(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    return (await session.execute(select(User).order_by(User.id))).scalars().all()


@router.put("/users/{user_id}/role", response_model=UserOut)
async def set_role(
    user_id: int,
    body: UserRoleUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Promote or demote someone.

    Refuses to remove the last admin — including when that is the caller
    demoting themselves. A platform with no admin has no way back short of
    editing the database, and the machine-lease and API-key surfaces are
    admin-gated.
    """
    if body.role not in (UserRole.ADMIN.value, UserRole.USER.value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "role must be admin|user")
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    if target.role == UserRole.ADMIN.value and body.role != UserRole.ADMIN.value:
        admins = (
            await session.execute(
                select(func.count(User.id)).where(User.role == UserRole.ADMIN.value)
            )
        ).scalar_one()
        if admins <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "this is the only admin — promote someone else first",
            )
    target.role = body.role
    session.add(
        Event(
            actor=admin.username,
            kind="user_role_changed",
            payload={"user": target.username, "role": body.role},
        )
    )
    await session.commit()
    await session.refresh(target)
    return target
