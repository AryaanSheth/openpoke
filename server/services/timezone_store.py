"""The user's preferred timezone, read from the tenant context.

Was ``data/timezone.txt`` — one global file for the whole process. It is now the
``users.timezone`` column.

``utils/timezones.py`` calls ``get_timezone()`` on **every** log append, so the
read path deliberately never touches the database: the value is carried inline on
``TenantContext`` and refreshed whenever it changes. Writes go to Postgres and
update the context in the same call.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..logging_config import logger
from ..repositories.context import get_tenant, require_tenant, run_sync, update_tenant
from ..repositories.users import UserRepository

DEFAULT_TIMEZONE = "UTC"


def _validate(timezone_name: str) -> str:
    candidate = (timezone_name or "").strip()
    if not candidate:
        raise ValueError("timezone must be a non-empty string")
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {candidate}") from exc
    return candidate


class TimezoneStore:
    """Tenant-scoped proxy. The module-level instance holds no state of its own.

    Kept as a class with the original method names because
    ``server/utils/timezones.py`` and ``agents/execution_agent/tools/triggers.py``
    call it and belong to other phases.
    """

    def get_timezone(self, default: str = DEFAULT_TIMEZONE) -> str:
        tenant = get_tenant()
        if tenant is None:
            # Background code that has not entered a tenant scope. Returning the
            # default is correct: there is no user whose preference could apply.
            return default
        return tenant.timezone or default

    def set_timezone(self, timezone_name: str) -> None:
        validated = _validate(timezone_name)
        tenant = require_tenant()

        async def _write(session) -> None:
            await UserRepository(session, tenant.user_id).set_timezone(validated)

        run_sync(_write)
        update_tenant(timezone=validated)
        logger.info(
            "updated timezone preference",
            extra={"user_id": str(tenant.user_id), "timezone": validated},
        )

    def clear(self) -> None:
        """Reset to the default. There is no "unset" state on the column."""
        self.set_timezone(DEFAULT_TIMEZONE)


_timezone_store = TimezoneStore()


def get_timezone_store() -> TimezoneStore:
    return _timezone_store


async def set_timezone_async(session, user_id, timezone_name: str) -> str:
    """Async path for routes, which already hold the request session."""
    validated = _validate(timezone_name)
    await UserRepository(session, user_id).set_timezone(validated)
    update_tenant(timezone=validated)
    return validated


async def get_timezone_async(session, user_id, default: str = DEFAULT_TIMEZONE) -> str:
    return await UserRepository(session, user_id).get_timezone(default)


__all__ = [
    "DEFAULT_TIMEZONE",
    "TimezoneStore",
    "get_timezone_async",
    "get_timezone_store",
    "set_timezone_async",
]
