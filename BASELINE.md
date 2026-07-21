# Baseline Setup — Getting OpenPoke Running Locally

**Status as of 2026-07-21: fully working.** Chat, Gmail search, execution-agent delegation,
and the importance watcher all verified end to end against a real mailbox.

This documents everything required to get from a fresh clone to a working local instance —
the code changes we had to make, the account-side prerequisites, and the gotchas that cost
time. Written before any architecture work, so it captures the **unmodified baseline**.

---

## TL;DR — clean setup

```bash
git clone <repo> && cd openpoke

# 1. Env file must be named .env (see Gotcha 1)
cp .env.example .env      # then fill in the three keys

# 2. Backend
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
.venv/bin/python -m server.server          # :8001

# 3. Frontend (separate terminal)
npm install --prefix web
npm run dev --prefix web                   # :3000
```

Then open http://localhost:3000 → **Settings → Gmail** → complete the Composio OAuth flow.

Verified on **Python 3.14.5**, **Node 24.14.1**, **npm 11.11.0** (macOS arm64). The README
says Python 3.10+; 3.14 works fine and all dependencies had prebuilt wheels.

---

## Code changes we made

Two files. Both were required — the app could not reach a working Gmail state without them.

### 1. `server/services/gmail/client.py` — composio SDK compatibility 🔴

**Symptom:** Gmail reported `{"connected": false, "status": "UNKNOWN"}` while Composio's own
API showed the account `ACTIVE`. Every Gmail feature was dead — drafting, replies, search,
and the importance watcher all gate on `get_active_gmail_user_id()`, which never populated.
No error appeared anywhere.

**Cause:** `requirements.txt` specified `composio>=0.5.0`. A clean install in July 2026
resolves **0.18.0**, which renamed the list-response payload:

```
ConnectedAccountListResponse
  .data   → no longer exists       ← what the code read
  .items  → the ACTIVE account     ← where the data now lives
```

`fetch_status` read `.data`, got `None`, and left `account = None`. The bare
`except Exception: account = None` swallowed all evidence.

**Fix:** accept both response shapes, and log the previously-swallowed exception.

```python
# composio renamed the list payload `data` -> `items` (>=0.18).
# Accept both so a version bump can't silently report "disconnected".
data = getattr(items, "data", None) or getattr(items, "items", None)
if data is None and isinstance(items, dict):
    data = items.get("data") or items.get("items")
```

> **Nobody wrote a bug.** The code was correct against composio 0.5. An unpinned `>=`
> upgraded the SDK underneath it and silently disabled the product's main feature.

### 2. `server/requirements.txt` — pinned all dependencies

All seven deps moved from unbounded `>=` to `==`, at the versions verified working:

```
fastapi==0.139.2          uvicorn[standard]==0.51.0    pydantic==2.13.4
httpx==0.28.1             python-dateutil==2.9.0.post0 beautifulsoup4==4.15.0
composio==0.18.0
```

Direct consequence of the bug above. Bump deliberately with tests green, never implicitly
at install time.

### Not a code change: `.env` symlink

`config.py:12` reads **only** `.env` from the repo root. We keep keys in `.env.local`, so:

```bash
ln -s .env.local .env     # both are gitignored
```

---

## Account-side prerequisites

None of these are code problems, but all three block a working instance.

| # | Requirement | Symptom if missing |
|---|---|---|
| 1 | **OpenRouter credits** | `402` — "requires more credits… requested up to 64000 tokens, but can only afford N". Chat returns `202` then silently never replies. |
| 2 | **Composio API key with `tool_execution: write`** | `403 APIKey_InsufficientPermissions` on every tool call. Gmail reads work, nothing executes. |
| 3 | **Gmail connected via Composio OAuth** | Agent replies "your gmail isn't connected". Do it in **Settings → Gmail**. |

### On the Composio key (#2)

There are **two independent permission layers**, easy to conflate:

| Layer | Controls | Where |
|---|---|---|
| **Google OAuth scopes** | What the end user's Google account lets Composio touch (`https://mail.google.com/` = full mailbox) | Composio → Auth Configs |
| **Composio API key permissions** | Whether *your key* may call `POST /tools/execute/*` at all | Composio → Settings → Project Settings → API Keys |

A **scoped project API key** can read connected accounts and toolkits while being unable to
execute anything. That is exactly the state we hit. `tool_execution` is a **resource-area**
permission covering all tool execution — it is *not* per-tool (there is no
`gmail-send-email` permission). The tool/execution **allowlist** is a third, separate thing:
it gates *which tools* may run, and is not consulted if the key can't execute at all.

**Fastest fix:** generate a default **project API key** (full access to one project) rather
than hunting for the individual toggle.

### Diagnostic: check the key in 2 seconds

```python
from composio import Composio
c = Composio(api_key="<key>")
c.connected_accounts.list()   # read
c.toolkits.get("gmail")       # read
c.client.tools.execute("GMAIL_FETCH_EMAILS", user_id="<uid>", arguments={"max_results": 1})
```

The third call is the one that needs `tool_execution: write`. If it 403s, the key is scoped.

---

## Gotchas

**1. `.env.local` is silently ignored.** `config.py:12` checks for `.env` and `return`s if
absent — no warning. The server starts fine, then fails on the first LLM call with
"API key not configured", which points at the wrong problem.

**2. Port 3000 conflicts move the app.** Next.js auto-increments to `:3001` if `:3000` is
taken, so a health check against `:3000` may hit an entirely different app. Check the
`npm run dev` output for the actual port.

**3. Your identity lives in browser `localStorage`.** The UI generates `openpoke_user_id`
(`SettingsModal.tsx:125`) and POSTs it to the server, which caches it in the process-global
`_ACTIVE_USER_ID`. Practical consequences:

- Clearing browser data loses the binding to your Gmail connection.
- A different browser/profile looks like a different user.
- **After a server restart the backend has amnesia** until a browser calls `/gmail/status`.
  The importance watcher does nothing until then.
- Any script or second process has its own `_ACTIVE_USER_ID` (i.e. `None`) — out-of-process
  Gmail calls will report "Gmail not connected" even when the server is fine.

To prime a server without a browser:

```bash
curl -X POST http://localhost:8001/api/v1/gmail/status \
  -H 'Content-Type: application/json' -d '{"user_id":"<your-uuid>"}'
```

**4. Stale conversation history poisons behavior.** After Gmail was fixed, the agent kept
replying *"your gmail isn't connected"* — it was pattern-matching on outdated replies still
in the transcript rather than calling the tool. `DELETE /api/v1/chat/history` fixed it
instantly. If the agent asserts something stale and confidently, suspect the transcript.

**5. `DELETE /chat/history` wipes everything.** Not just the conversation — execution agent
logs, the agent roster, and **all stored triggers**, unauthenticated. Fine locally; know
what it does before using it.

**6. Failures are invisible by design.** `POST /chat/send` returns `202` immediately and runs
the work in a detached task. If that work fails, nothing reaches the client — the UI polls,
gives up, and clears the spinner with no error.

**7. Server logs drop all structured context.** `logging_config.py` formats with
`%(message)s`, so every `extra={...}` is discarded. An OpenRouter `402` surfaces as exactly
`ERROR - Interaction agent failed`. To debug, run the server with a formatter that prints
`record.__dict__`, or reproduce the call in a script.

---

## Verified working

```
backend  :8001  200   {"ok":true,"service":"openpoke","version":"0.3.0"}
frontend :3000  200
gmail           connected=true  avsheth03@gmail.com  (21,980 messages)
LLM             anthropic/claude-sonnet-4 via OpenRouter, $0.000096/call
```

End-to-end chat exercising the full agent chain:

> **User:** Summarize my 2 most recent emails, one line each.
>
> **Assistant:** I'll check your recent emails and get you those summaries.
>
> **Assistant:** **Most recent:** Product Hunt Daily newsletter featuring AI tools including
> Ditto, tterm, and Routebase. **Second most recent:** Adobe Acrobat promotional email
> highlighting PDF editing features.

Chain fired: interaction agent → `send_message_to_agent` → "Email Summary" execution agent →
`task_email_search` → Composio → Gmail → summary.

---

## Note on model configuration

`config.py:54-58` hardcodes `anthropic/claude-sonnet-4` for all five agent roles, with no env
override. Sonnet 4 retired on Anthropic's **first-party** API on 2026-06-15, but **OpenRouter
still serves it** — verified live. Not an outage, but the IDs should become env-overridable
and move to a current model deliberately. See `plan.md` Phase 0.

Also worth knowing: `openrouter_client/client.py` never sets `max_tokens`, so OpenRouter
reserves the model's full output ceiling (64k) on every request. That is what makes a small
credit balance fail immediately, and it is a real cost-control gap at scale.
