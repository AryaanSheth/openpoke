# Models

## Current state — env-overridable, all five default to Sonnet

All five model settings (`server/config.py`) resolve from the environment; none are
hardcoded with no override anymore (they were, before Phase 0). Every one currently
defaults to `anthropic/claude-sonnet-4`.

| Setting | Env var | Default |
|---|---|---|
| `interaction_agent_model` | `OPENPOKE_INTERACTION_AGENT_MODEL` | `anthropic/claude-sonnet-4` |
| `execution_agent_model` | `OPENPOKE_EXECUTION_AGENT_MODEL` | `anthropic/claude-sonnet-4` |
| `execution_agent_search_model` | `OPENPOKE_EXECUTION_AGENT_SEARCH_MODEL` | `anthropic/claude-sonnet-4` |
| `summarizer_model` | `OPENPOKE_SUMMARIZER_MODEL` | `anthropic/claude-sonnet-4` |
| `email_classifier_model` | `OPENPOKE_EMAIL_CLASSIFIER_MODEL` | `anthropic/claude-sonnet-4` |

**Why the default didn't change even though the model nominally retired.**
`claude-sonnet-4` (`claude-sonnet-4-20250514`) retired on Anthropic's first-party API on
2026-06-15. This repo calls through OpenRouter, not the first-party API, and
`BASELINE.md` verified with one live call that OpenRouter still serves that ID — a
provenance-D fact (verified externally), not an assumption. Keeping the default as-is
was a deliberate choice, not an oversight: changing a production default needs the same
evidence a routing change needs (below), and today that evidence doesn't exist yet.

## Proposed per-role routing — a proposal, not applied config

`plan.md` Phase 0 step 3b proposes routing three of the five roles to Haiku 4.5 instead
of Sonnet. **This is documented here as a proposal gated on behavioral fixtures. No
model default has been changed to Haiku anywhere in this codebase.** The gate is
`tests/test_behavioral.py` (recorded LLM-response fixtures asserting the tool-call
sequence for representative scenarios) — the harness for verifying a downgrade doesn't
change what the agent actually does before it ships, not after.

| Setting | Proposed model | Why | Status |
|---|---|---|---|
| `email_classifier_model` | Haiku | Binary classify + short summary against a fixed tool schema. Highest call volume by far — this one line is most of the proposed saving. | Proposed |
| `summarizer_model` | Haiku | Single-call transcript compression, no tool loop, low quality bar. | Proposed |
| `execution_agent_search_model` | Haiku, **pending validation** | Mostly filtering/extraction over email search results — plausibly Haiku-shaped, but plan.md explicitly flags this one as needing fixture validation before committing, unlike the two above. | Proposed, needs validation |
| `execution_agent_model` | Sonnet (unchanged) | Multi-step tool loop with side effects — it sends real email. Not proposed to move. | Unchanged |
| `interaction_agent_model` | Sonnet (unchanged) | Orchestrator: 8-iteration tool loop, decides delegation, writes the user-facing text. The one place output quality is the product. Not proposed to move. | Unchanged |

**Order of operations, per the plan:** build the behavioral fixtures first, then route a
role to Haiku with the fixture's tool-call-sequence assertion as evidence the behavior
didn't change — not the other way around. Flipping a default and hoping the fixtures
(written later) happen to still pass is exactly the "vibes" the plan explicitly rejects.

## The context-window constraint this creates

Haiku 4.5's context window is 200K tokens, against Sonnet's 1M. That is the deciding
constraint on any role that carries growing history in its prompt — which is exactly
what makes `plan.md`'s Phase 2 step 8 prompt cap (a bounded, tail-preserving window on
execution-agent history, replacing the previous unbounded `conversation_limit=None`)
**more urgent under this proposal, not less**: on Sonnet's 1M window the unbounded-growth
fuse referenced below takes days to blow; on Haiku's 200K window the same growth curve
hits the ceiling roughly 5× sooner. Adopting Haiku for any history-carrying role without
that cap already in place would make the failure mode worse, not just cheaper per call.

## Cost reasoning — carried forward with its own caveat, not restated as measurement

`plan.md`'s appendix labels every number below **tier B (arithmetic) resting on tier C
inputs (unmeasured assumptions)** — call volume, tokens/call, and the $/day burn rate are
all order-of-magnitude arguments, not forecasts. Repeating that labeling here rather than
dropping it, because the numbers read as far more precise than they are once they're
lifted out of the appendix that qualifies them:

- Sonnet-tier pricing: $3/M input, $15/M output. Haiku 4.5: $1/M input, $5/M output — 3×
  cheaper on both sides. (Tier D — checked against Anthropic's published pricing.)
- At an assumed 10k users, ~50 classified emails/user/day at ~1k tokens each: roughly
  500k classifier calls/day. At Sonnet pricing that's on the order of **$2.2k/day**; at
  Haiku, on the order of **$0.7k/day** — a difference on the order of **$45k/month from
  one config line**, if the volume assumption holds.
- The unbounded-prompt fuse this same cap addresses (Phase 2 step 8, not a Phase 6
  change): a trigger agent firing every 5 minutes with no cap appends roughly 1KB/fire;
  by day 3 its system prompt is on the order of 200k tokens, roughly **$0.60/fire ≈
  $170/day for one agent**, then it crosses the context window and every subsequent call
  fails outright. This number rests on the same class of unmeasured inputs (bytes/fire,
  chars/token) as the classifier estimate above.

**Treat every figure in this section as "this becomes a real line item, order of
magnitude X" — not as a forecast.** The plan's own appendix deletes rather than defends
any number it can't source, and keeps these specifically as arithmetic-from-assumptions,
flagged as such, because the conclusion (cap the prompt; consider routing the classifier)
holds across a wide range of the underlying inputs even though the inputs themselves
aren't measured.

**What replaces the estimate with a fact:** `plan.md` Phase 2 step 9 records
`prompt_tokens`, `completion_tokens`, `model`, `user_id`, and `job_id` to an `llm_usage`
table on every OpenRouter response (previously discarded). Once that's live, "$45k/month"
stops being an argument and becomes a query — `SELECT sum(...) FROM llm_usage WHERE
model = ...` replaces every number in this section within a day of launch. That table,
not this document, is the actual source of truth going forward.
