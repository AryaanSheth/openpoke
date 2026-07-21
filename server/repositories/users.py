"""Users: creation, API-key minting, and the per-tenant timezone preference."""

from __future__ import annotations

import secrets
import uuid

from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import User

#: Bearer tokens look like ``opk_<user-uuid-hex>_<secret>``. The embedded user id
#: is what makes verification a single indexed primary-key lookup plus one argon2
#: check, instead of an argon2 check against every row in ``users``.
TOKEN_PREFIX = "opk"
_SECRET_BYTES = 32

#: Interactive-ish parameters. Argon2's whole point is to be slow; see
#: ``server/auth.py`` for how the per-request cost is handled.
_hasher = PasswordHasher()


def mint_token(user_id: uuid.UUID) -> tuple[str, str]:
    """Return ``(token, argon2_hash)`` for a new API key."""
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    token = f"{TOKEN_PREFIX}_{user_id.hex}_{secret}"
    return token, _hasher.hash(secret)


def split_token(token: str) -> tuple[uuid.UUID, str] | None:
    """Parse ``opk_<hex>_<secret>``. Returns ``None`` for anything malformed."""
    parts = token.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    try:
        user_id = uuid.UUID(hex=parts[1])
    except ValueError:
        return None
    if not parts[2]:
        return None
    return user_id, parts[2]


def verify_secret(secret: str, api_key_hash: str) -> bool:
    """Constant-time argon2 verification. Never raises on a bad key."""
    try:
        return bool(_hasher.verify(api_key_hash, secret))
    except Exception:
        return False


async def create_user(
    session: AsyncSession, *, email: str, timezone: str = "UTC"
) -> tuple[User, str]:
    """Create a user and return ``(user, plaintext_token)``.

    The plaintext token is returned exactly once and never stored. There is
    deliberately no signup API — this is called by ``python -m server.auth``
    and by tests.
    """
    user_id = uuid.uuid4()
    token, api_key_hash = mint_token(user_id)
    user = User(id=user_id, email=email, api_key_hash=api_key_hash, timezone=timezone)
    session.add(user)
    await session.flush()
    return user, token


async def rotate_token(session: AsyncSession, user: User) -> str:
    """Issue a new API key for *user*, invalidating the old one."""
    token, api_key_hash = mint_token(user.id)
    user.api_key_hash = api_key_hash
    await session.flush()
    return token


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


class UserRepository:
    """Tenant-scoped view of the caller's own ``users`` row."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id

    async def get(self) -> User | None:
        result = await self._session.execute(select(User).where(User.id == self._user_id))
        return result.scalar_one_or_none()

    async def get_timezone(self, default: str = "UTC") -> str:
        result = await self._session.execute(
            select(User.timezone).where(User.id == self._user_id)
        )
        return result.scalar_one_or_none() or default

    async def set_timezone(self, timezone_name: str) -> str:
        user = await self.get()
        if user is None:
            raise LookupError("user not found")
        user.timezone = timezone_name
        await self._session.flush()
        return timezone_name


__all__ = [
    "TOKEN_PREFIX",
    "UserRepository",
    "create_user",
    "get_user_by_email",
    "mint_token",
    "rotate_token",
    "split_token",
    "verify_secret",
]
