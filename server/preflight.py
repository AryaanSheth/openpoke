"""Startup validation for the integration surface.

Every one of these checks exists because the real thing broke and reported
something misleading. Chronologically, on this repo:

1. ``composio>=0.5.0`` resolved to 0.18, which renamed the list payload
   ``.data`` -> ``.items``. A bare ``except Exception`` swallowed it, Gmail
   reported "disconnected" forever, and no error appeared anywhere.
2. A scoped API key could read connected accounts but not execute tools: every
   Gmail action 403'd, reads kept working, so it looked like a Gmail problem.
3. The same key could not create connections. "Failed to initiate Gmail connect."
4. Composio deprecated ``connected_accounts.initiate`` for managed OAuth configs
   in favour of ``link``; the old call started returning 400.

The test suite structurally cannot catch any of these — it stubs Composio, so by
construction it never sees the real API change underneath it. That is the gap
this file fills.

    python -m server.preflight          # all checks, exit 1 on any failure
    python -m server.preflight --fast   # config + SDK shape only, no network

# ponytail: warn-and-continue by default, because a degraded integration should
# not stop the API from serving /health or anything that doesn't touch Gmail.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from .config import get_settings
from .logging_config import logger


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = False


def _config_checks() -> list[Check]:
    s = get_settings()
    out: list[Check] = []
    for label, value, fatal in (
        ("OPENROUTER_API_KEY", s.openrouter_api_key, True),
        ("COMPOSIO_API_KEY", s.composio_api_key, False),
        ("COMPOSIO_GMAIL_AUTH_CONFIG_ID", s.composio_gmail_auth_config_id, False),
    ):
        out.append(
            Check(
                f"config: {label}",
                bool(value),
                "set" if value else "missing — the feature that needs it will fail at call time",
                fatal,
            )
        )
    return out


def _sdk_shape_check() -> Check:
    """Guard the exact drift that silently disabled Gmail once already."""
    try:
        from composio.core.models.connected_accounts import ConnectedAccounts
    except Exception as exc:
        return Check("composio: SDK importable", False, f"{type(exc).__name__}: {exc}", True)

    # `list` is resolved dynamically on the instance, so it is verified by the
    # read probe below rather than by hasattr on the class.
    missing = [m for m in ("link", "initiate") if not hasattr(ConnectedAccounts, m)]
    if missing:
        return Check(
            "composio: SDK surface",
            False,
            f"missing {missing} — the SDK changed shape; see initiate/link handling in gmail/client.py",
        )
    return Check("composio: SDK surface", True, "list/link/initiate all present")


def _composio_permission_checks() -> list[Check]:
    """Read is not enough. The key needs write on connections and tool execution."""
    s = get_settings()
    if not s.composio_api_key:
        return [Check("composio: reachable", False, "no API key configured")]

    try:
        from composio import Composio

        client = Composio(api_key=s.composio_api_key)
    except Exception as exc:
        return [Check("composio: client", False, f"{type(exc).__name__}: {exc}")]

    out: list[Check] = []
    try:
        items = client.connected_accounts.list()
        # The 0.18 rename, handled explicitly rather than by a bare except.
        data = getattr(items, "data", None)
        if data is None:
            data = getattr(items, "items", None)
        if data is None and isinstance(items, dict):
            data = items.get("data") or items.get("items")
        if data is None:
            out.append(
                Check(
                    "composio: list payload shape",
                    False,
                    "neither .data nor .items — SDK renamed the payload again",
                )
            )
        else:
            active = [a for a in data if getattr(a, "status", None) == "ACTIVE"]
            out.append(
                Check(
                    "composio: connected_accounts read",
                    True,
                    f"{len(data)} account(s), {len(active)} ACTIVE",
                )
            )
    except Exception as exc:
        out.append(Check("composio: connected_accounts read", False, _short(exc)))
        return out

    # Write permission, checked without creating anything: a deliberately invalid
    # auth_config_id returns 4xx either way, but a *permissions* error is distinct
    # and is the thing we actually want to surface.
    try:
        client.connected_accounts.link(user_id="preflight-probe", auth_config_id="ac_preflight_invalid")
        out.append(Check("composio: connections write", True, "permitted"))
    except Exception as exc:
        msg = str(exc)
        if "InsufficientPermissions" in msg or "does not have the permissions" in msg:
            out.append(
                Check(
                    "composio: connections write",
                    False,
                    "API key is READ-ONLY for connected_accounts — no user can connect Gmail. "
                    "Generate a default project API key in Composio settings.",
                )
            )
        else:
            # Any other error means the permission itself was fine.
            out.append(Check("composio: connections write", True, "permitted (probe rejected as expected)"))
    return out


def _short(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return f"{type(exc).__name__}: {text[:160]}"


def _database_checks() -> list[Check]:
    import asyncio

    from sqlalchemy import text

    from .db.engine import get_engine

    async def _probe() -> list[Check]:
        out: list[Check] = []
        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            out.append(Check("database: reachable", True, "SELECT 1 OK"))
            out.append(
                Check(
                    "database: migrated",
                    bool(rev),
                    f"alembic at {rev}" if rev else "no alembic_version row — run `alembic upgrade head`",
                    fatal=not rev,
                )
            )
        except Exception as exc:
            out.append(Check("database: reachable", False, _short(exc), fatal=True))
        return out

    return asyncio.run(_probe())


def run(fast: bool = False) -> list[Check]:
    checks = _config_checks()
    checks.append(_sdk_shape_check())
    if not fast:
        checks.extend(_database_checks())
        checks.extend(_composio_permission_checks())
    return checks


def log_summary(checks: list[Check]) -> None:
    for c in checks:
        if not c.ok:
            logger.warning("preflight: %s — %s", c.name, c.detail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate the integration surface.")
    ap.add_argument("--fast", action="store_true", help="config + SDK shape only, no network")
    args = ap.parse_args(argv)

    checks: list[Check] = run(fast=args.fast)
    green, red = "\033[32m", "\033[31m"
    dim, off = "\033[2m", "\033[0m"

    print()
    for c in checks:
        mark = f"{green}PASS{off}" if c.ok else f"{red}FAIL{off}"
        print(f"  [{mark}] {c.name}\n         {dim}{c.detail}{off}")

    failed = [c for c in checks if not c.ok]
    print()
    if not failed:
        print(f"  {green}all {len(checks)} checks passed{off}\n")
        return 0
    print(f"  {red}{len(failed)}/{len(checks)} failed{off}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
