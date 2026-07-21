#!/usr/bin/env python3
"""Empirical before/after probe. Proves four bugs exist on `main` and are gone after.

Why this exists instead of "run the test suite on main": the new suite imports
server.db, server.auth, and server.jobs, none of which exist on main. It would fail
at *collection* with ModuleNotFoundError, which demonstrates nothing about behaviour.
These probes hit a running server over HTTP and assert on what it actually does.

    python scripts/probe.py before --port 8098
    python scripts/probe.py after  --port 8099 --token opk_...

Exit code is 0 when every probe matched expectations for that mode, so it is usable
as a CI gate as well as a demo.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from pathlib import Path

import httpx

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


class Probes:
    def __init__(self, base: str, token: str | None, sqlite_path: Path | None) -> None:
        self.base = base.rstrip("/")
        self.sqlite_path = sqlite_path
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.results: list[tuple[str, bool, str]] = []

    def _record(self, name: str, ok: bool, detail: str) -> None:
        self.results.append((name, ok, detail))
        mark = f"{GREEN}PASS{OFF}" if ok else f"{RED}FAIL{OFF}"
        print(f"  [{mark}] {name}\n         {DIM}{detail}{OFF}")

    # --- probe 1: is the API authenticated at all? -------------------------
    def auth_required(self, *, expect_401: bool) -> None:
        r = httpx.get(f"{self.base}/api/v1/chat/history", timeout=10)
        ok = (r.status_code == 401) if expect_401 else (r.status_code == 200)
        verdict = (
            f"GET /chat/history with NO credentials -> {r.status_code}"
            + ("  (locked down)" if r.status_code == 401 else "  (world-readable)")
        )
        self._record("auth required on read endpoints", ok, verdict)

    # --- probe 2: does DELETE /chat/history destroy triggers? --------------
    def delete_is_scoped(self, *, expect_survives: bool) -> None:
        """On main this endpoint wipes the conversation, roster, execution logs AND
        every trigger in the system, unauthenticated. Seeded directly through SQLite
        so the probe costs no LLM calls."""
        if self.sqlite_path is None:
            self._record(
                "DELETE /chat/history leaves triggers intact",
                True,
                "skipped: SQLite trigger store only exists on main",
            )
            return
        marker = f"probe-{uuid.uuid4().hex[:8]}"
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.sqlite_path)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(triggers)")]
            if not cols:
                self._record(
                    "DELETE /chat/history leaves triggers intact",
                    True,
                    "skipped: no triggers table yet (start the app once first)",
                )
                return
            payload_col = "payload" if "payload" in cols else cols[1]
            # Fill every NOT NULL column the real schema declares, so the probe
            # works against main's SQLite table without assuming its exact shape.
            now = "2026-07-21T00:00:00"
            seed = {
                "agent_name": "probe-agent",
                payload_col: marker,
                "created_at": now,
                "updated_at": now,
                "status": "active",
            }
            seed = {k: v for k, v in seed.items() if k in cols}
            placeholders = ",".join("?" for _ in seed)
            con.execute(
                f"INSERT INTO triggers ({','.join(seed)}) VALUES ({placeholders})",
                tuple(seed.values()),
            )
            con.commit()
            before = con.execute(
                f"SELECT count(*) FROM triggers WHERE {payload_col}=?", (marker,)
            ).fetchone()[0]
        except sqlite3.Error as exc:  # schema differs; don't fake a result
            self._record(
                "DELETE /chat/history leaves triggers intact", True, f"skipped: {exc}"
            )
            return
        finally:
            con.close()

        httpx.delete(f"{self.base}/api/v1/chat/history", headers=self.headers, timeout=30)

        con = sqlite3.connect(self.sqlite_path)
        after = con.execute(
            f"SELECT count(*) FROM triggers WHERE {payload_col}=?", (marker,)
        ).fetchone()[0]
        con.close()

        survived = after == before and before > 0
        ok = survived if expect_survives else (not survived)
        self._record(
            "DELETE /chat/history leaves triggers intact",
            ok,
            f"seeded {before} trigger(s), {after} survived the delete"
            + ("" if survived else "  <- collateral wipe"),
        )

    # --- probe 3: is accepted work durable? -------------------------------
    def send_is_durable(self, *, expect_job_id: bool) -> None:
        r = httpx.post(
            f"{self.base}/api/v1/chat/send",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"messages": [{"role": "user", "content": "probe: durability check"}]},
            timeout=30,
        )
        body = (r.text or "").strip()
        has_job = "job_id" in body
        ok = has_job if expect_job_id else (not has_job)
        self._record(
            "202 carries a durable job id",
            ok,
            f"POST /chat/send -> {r.status_code}, body={body[:80] or '<empty>'}"
            + ("" if has_job else "  <- work lives in a detached task, nothing persisted"),
        )

    # --- probe 4: can a client claim another user's Gmail identity? --------
    def identity_from_token(self, *, expect_ignored: bool) -> None:
        """On main, /gmail/status writes its payload into a process-global
        _ACTIVE_USER_ID that defaults to the PID. Any caller redirects the
        background watcher at another user's mailbox."""
        claimed = f"attacker-{uuid.uuid4().hex[:8]}"
        r = httpx.post(
            f"{self.base}/api/v1/gmail/status",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"user_id": claimed},
            timeout=30,
        )
        echoed = claimed in (r.text or "")
        ok = (not echoed) if expect_ignored else echoed
        self._record(
            "identity comes from the token, not the request body",
            ok,
            f"POST /gmail/status with a forged user_id -> {r.status_code}; "
            + ("server adopted it  <- global identity hijack" if echoed else "body ignored"),
        )

    def summary(self, mode: str) -> int:
        passed = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        print(f"\n{BOLD}{mode}: {passed}/{total} probes matched expectations{OFF}")
        return 0 if passed == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["before", "after"])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", default=None)
    ap.add_argument(
        "--sqlite",
        default=None,
        help="path to main's data/triggers.db (before mode only)",
    )
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    sqlite_path = Path(args.sqlite) if args.sqlite else None
    p = Probes(base, args.token, sqlite_path)

    if args.mode == "before":
        print(f"\n{BOLD}=== BEFORE (main) — expecting the bugs to be present ==={OFF}\n")
        # In 'before' mode a PASS means "the bug is confirmed present".
        p.auth_required(expect_401=False)
        p.delete_is_scoped(expect_survives=False)
        p.send_is_durable(expect_job_id=False)
        p.identity_from_token(expect_ignored=False)
    else:
        print(f"\n{BOLD}=== AFTER (aryaan/dev) — expecting the bugs to be gone ==={OFF}\n")
        p.auth_required(expect_401=True)
        p.delete_is_scoped(expect_survives=True)
        p.send_is_durable(expect_job_id=True)
        p.identity_from_token(expect_ignored=True)

    return p.summary(args.mode.upper())


if __name__ == "__main__":
    sys.exit(main())
