"""Registration, login, session, and password endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.container import Container
from app.core.security import create_access_token, hash_password, verify_password
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.gateway.dependencies import (
    ContainerDep,
    CurrentUserDep,
    SessionDep,
)
from app.gateway.schemas import (
    LoginRequest,
    PasswordChangeRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: User, container: Container) -> TokenResponse:
    settings = container.settings
    return TokenResponse(
        access_token=create_access_token(user.id, settings),
        token_type="bearer",
        expires_in=settings.auth_token_ttl_seconds,
        user=UserResponse.model_validate(user),
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    body: RegisterRequest,
    db: SessionDep,
    container: ContainerDep,
) -> TokenResponse:
    """Create an account and return a signed access token.

    The caller is authenticated immediately: registration and login are the
    same transaction so a fresh client has nothing extra to do.
    """
    repository = UserRepository(db)
    if await repository.get_by_email(body.email) is not None:
        raise HTTPException(status_code=409, detail="email already registered")
    user = User(
        email=body.email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
    )
    await repository.add(user)
    await db.commit()
    return _token_response(user, container)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: SessionDep,
    container: ContainerDep,
) -> TokenResponse:
    """Verify credentials and return a signed access token.

    Failing credentials are indistinguishable from a missing account. Local
    users with no password hash (for example the CLI's internal user) can
    never authenticate over HTTP.
    """
    user = await UserRepository(db).get_by_email(body.email)
    if (
        user is None
        or user.hashed_password is None
        or not verify_password(body.password, user.hashed_password)
    ):
        raise HTTPException(status_code=401, detail="incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account disabled")
    return _token_response(user, container)


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUserDep) -> User:
    """Return the authenticated user's profile."""
    return current_user


@router.post("/password", response_model=UserResponse)
async def change_password(
    body: PasswordChangeRequest,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> User:
    """Replace the authenticated user's password after verifying the current one."""
    if current_user.hashed_password is None or not verify_password(
        body.current_password, current_user.hashed_password
    ):
        raise HTTPException(status_code=400, detail="current password is incorrect")
    current_user.hashed_password = hash_password(body.new_password)
    await db.commit()
    await db.refresh(current_user)
    return current_user
