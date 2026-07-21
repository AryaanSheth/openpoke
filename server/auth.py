"""Bearer-token authentication and tenant binding.

``Authorization: Bearer opk_<user-uuid-hex>_<secret>`` → argon2-verify the secret
against ``users.api_key_hash`` → ``User``. Missing or bad ⇒ 401. Cross-tenant
resources ⇒ 404, never 403; see the repositories.

**The argon2 cost, and what was done about it.** Argon2 is deliberately slow —
that is the whole point of a KDF, and it is why a leaked ``api_key_hash`` is not
a leaked key. But a naive implementation pays that cost on *every* authenticated
request, which turns a 2 ms endpoint into a 60 ms one and hands an attacker a
cheap CPU-exhaustion vector (unauthenticated requests would each burn a full
hash).

The fix here is a **bounded, TTL'd verification cache**: ``sha256(token)`` →
``user_id``, at most ``_CACHE_MAX`` entries, evicted LRU, each valid for
``_CACHE_TTL_S``. On a hit we skip argon2 but still read the ``users`` row by
primary key, so a deleted user is rejected immediately.

The tradeoff, stated plainly: **a rotated or revoked API key keeps working for up
to ``_CACHE_TTL_S`` seconds on any process that had already seen it.** Five
minutes of stale credential is the price of not paying a KDF per request. If that
is unacceptable, drop ``_CACHE_TTL_S`` to 0 — the cache is the only thing that
changes, nothing else depends on it. What we explicitly did *not* do is add an
unbounded cache (a memory-growth DoS keyed on attacker-supplied tokens) or
weaken the argon2 parameters (which weakens the at-rest guarantee, permanently).

Failed verifications are never cached, so a wrong token always costs a full
hash. That is intentional: it is the rate limiter.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import time
import uuid
from collections import OrderedDict
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import User
from .db.session import get_session
from .logging_config import logger
from .repositories.context import TenantContext, set_tenant
from .repositories.users import split_token, verify_secret

_bearer = HTTPBearer(auto_error=False)

_CACHE_MAX = 1024
_CACHE_TTL_S = 300.0
_cache: OrderedDict[str, tuple[uuid.UUID, float]] = OrderedDict()


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_get(token: str) -> uuid.UUID | None:
    key = _cache_key(token)
    entry = _cache.get(key)
    if entry is None:
        return None
    user_id, expires_at = entry
    if expires_at < time.monotonic():
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return user_id


def _cache_put(token: str, user_id: uuid.UUID) -> None:
    key = _cache_key(token)
    _cache[key] = (user_id, time.monotonic() + _CACHE_TTL_S)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_token_cache() -> None:
    """Drop every cached verification. Called by tests and by key rotation."""
    _cache.clear()


def _unauthorized() -> HTTPException:
    # One message for every failure mode — a missing header, a malformed token,
    # an unknown user and a wrong secret must be indistinguishable.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False)),
) -> User:
    """Resolve the bearer token to a ``User`` and bind the tenant context.

    Binding the context here (rather than in middleware) is deliberate:
    Starlette's ``BaseHTTPMiddleware`` runs in a different context than the
    endpoint, so a ``ContextVar`` set there would not be visible to the handler
    or to the tasks it spawns. A dependency shares the request's context.
    """
    if credentials is None or (credentials.scheme or "").lower() != "bearer":
        raise _unauthorized()

    token = (credentials.credentials or "").strip()
    if not token:
        raise _unauthorized()

    parsed = split_token(token)
    if parsed is None:
        raise _unauthorized()
    user_id, secret = parsed

    cached_id = _cache_get(token)

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise _unauthorized()

    if cached_id != user.id:
        if not verify_secret(secret, user.api_key_hash):
            logger.warning("rejected bearer token", extra={"user_id": str(user_id)})
            raise _unauthorized()
        _cache_put(token, user.id)

    set_tenant(
        TenantContext(
            user_id=user.id,
            timezone=user.timezone or "UTC",
            composio_user_id=str(user.id),
        )
    )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# CLI — `python -m server.auth`
#
# Deliberately not an HTTP signup endpoint: user provisioning is out of scope,
# but tests and local development still need a real user and a real token.
# ---------------------------------------------------------------------------


async def _cli_create(email: str, timezone_name: str) -> int:
    from .db.engine import dispose_engine, get_sessionmaker
    from .repositories.users import create_user, get_user_by_email

    async with get_sessionmaker()() as session:
        if await get_user_by_email(session, email) is not None:
            print(f"error: a user with email {email!r} already exists")
            await dispose_engine()
            return 1
        user, token = await create_user(session, email=email, timezone=timezone_name)
        await session.commit()
        print(f"user_id: {user.id}")
        print(f"token:   {token}")
        print("\nStore the token now — only its argon2 hash is kept.")
    await dispose_engine()
    return 0


async def _cli_rotate(email: str) -> int:
    from .db.engine import dispose_engine, get_sessionmaker
    from .repositories.users import get_user_by_email, rotate_token

    async with get_sessionmaker()() as session:
        user = await get_user_by_email(session, email)
        if user is None:
            print(f"error: no user with email {email!r}")
            await dispose_engine()
            return 1
        token = await rotate_token(session, user)
        await session.commit()
        print(f"user_id: {user.id}")
        print(f"token:   {token}")
        print(
            f"\nThe previous token may still be accepted for up to "
            f"{int(_CACHE_TTL_S)}s by already-running processes."
        )
    await dispose_engine()
    return 0


async def _cli_list() -> int:
    from .db.engine import dispose_engine, get_sessionmaker

    async with get_sessionmaker()() as session:
        rows = await session.execute(select(User.id, User.email, User.timezone).order_by(User.created_at))
        for user_id, email, tz in rows.all():
            print(f"{user_id}  {email}  {tz}")
    await dispose_engine()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m server.auth")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="create a user and mint an API token")
    create.add_argument("--email", required=True)
    create.add_argument("--timezone", default="UTC")

    rotate = sub.add_parser("rotate", help="issue a new token for an existing user")
    rotate.add_argument("--email", required=True)

    sub.add_parser("list", help="list users")

    args = parser.parse_args()
    if args.command == "create-user":
        return asyncio.run(_cli_create(args.email, args.timezone))
    if args.command == "rotate":
        return asyncio.run(_cli_rotate(args.email))
    return asyncio.run(_cli_list())


if __name__ == "__main__":  # pragma: no cover - CLI invocation guard
    raise SystemExit(main())


__all__ = ["CurrentUser", "clear_token_cache", "get_current_user"]
