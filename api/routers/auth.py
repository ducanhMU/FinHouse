"""FinHouse — Authentication Router."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import get_db
from models import User

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)


# ── Schemas ─────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    user_name: str
    user_password: str

class LoginRequest(BaseModel):
    user_name: str
    user_password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: int
    user_name: str

class RefreshRequest(BaseModel):
    refresh_token: str


# ── Helpers ─────────────────────────────────────────────────

def create_token(user_id: int, token_type: str = "access") -> str:
    if token_type == "access":
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.JWT_ACCESS_EXPIRE_MINUTES
        )
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            days=settings.JWT_REFRESH_EXPIRE_DAYS
        )
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> int:
    """
    Extract user_id from JWT. Returns 0 (guest) if no token provided.
    """
    if credentials is None:
        return 0
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id = int(payload["sub"])
        if user_id == 0:
            raise HTTPException(status_code=401, detail="Guest cannot have tokens")
        return user_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ── Endpoints ───────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    import logging
    log = logging.getLogger("finhouse.auth")

    # Normalize: strip whitespace + lowercase for case-insensitive uniqueness
    username = (body.user_name or "").strip().lower()
    password = body.user_password or ""

    log.info(f"register attempt: username={username!r} len={len(username)}")

    if len(username) < 3 or len(username) > 64:
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-64 characters (after trim)",
        )
    if len(password) < 8 or len(password) > 256:
        raise HTTPException(
            status_code=400,
            detail="Password must be 8-256 characters",
        )

    # Check existing (case-insensitive via lowercased username)
    result = await db.execute(
        select(User).where(User.user_name == username)
    )
    existing = result.scalar_one_or_none()
    if existing:
        log.warning(
            f"register rejected — username already exists "
            f"(existing user_id={existing.user_id}, requested={username!r})"
        )
        raise HTTPException(status_code=409, detail="Username already taken")

    # Let Postgres SERIAL assign user_id atomically — no race condition.
    user = User(
        user_name=username,
        user_password=pwd_context.hash(password),
    )
    db.add(user)
    try:
        await db.flush()
        log.info(f"register success: user_id={user.user_id} username={username!r}")
    except Exception as e:
        # Concurrent registrations with same username would hit UNIQUE constraint
        await db.rollback()
        log.error(f"register db error for {username!r}: {e}")
        raise HTTPException(status_code=409, detail="Username already taken") from e
    return {"user_id": user.user_id, "user_name": user.user_name}


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    import logging
    log = logging.getLogger("finhouse.auth")

    username = (body.user_name or "").strip().lower()
    log.info(f"login attempt: username={username!r}")

    result = await db.execute(
        select(User).where(User.user_name == username)
    )
    user = result.scalar_one_or_none()
    if not user or not user.user_password:
        log.warning(f"login rejected — user not found: {username!r}")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not pwd_context.verify(body.user_password or "", user.user_password):
        log.warning(f"login rejected — wrong password for user_id={user.user_id}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    log.info(f"login success: user_id={user.user_id} username={user.user_name!r}")
    return TokenResponse(
        access_token=create_token(user.user_id, "access"),
        refresh_token=create_token(user.user_id, "refresh"),
        user_id=user.user_id,
        user_name=user.user_name,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        payload = jwt.decode(
            body.refresh_token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Not a refresh token")
        user_id = int(payload["sub"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return TokenResponse(
        access_token=create_token(user.user_id, "access"),
        refresh_token=create_token(user.user_id, "refresh"),
        user_id=user.user_id,
        user_name=user.user_name,
    )
