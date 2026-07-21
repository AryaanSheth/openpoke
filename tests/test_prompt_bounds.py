"""The execution-agent prompt is bounded, and bounded from the correct end.

The failure this guards is not gradual. A trigger agent firing every 5 minutes
appends ~1KB per fire; by day 3 the system prompt is ~200k tokens and shortly
after that it exceeds the context window, at which point **every call fails
permanently**. A fuse, not a curve — so the test that matters is the one that
runs the growth out far enough to blow it.
"""

from __future__ import annotations

import pytest

from server.agents.execution_agent.agent import ELISION, ExecutionAgent, window_transcript


def _transcript(n: int, *, payload_size: int = 200) -> str:
    """*n* request/response pairs in the real ``render_entry`` shape."""
    lines = []
    for i in range(n):
        body = "task" if i == 0 else f"step {i} " + "x" * payload_size
        lines.append(f'<agent_request timestamp="2026-07-21 09:{i:02d}:00">{body}</agent_request>')
        lines.append(
            f'<agent_response timestamp="2026-07-21 09:{i:02d}:30">did {i}</agent_response>'
        )
    return "\n".join(lines)


def test_unbounded_is_still_available_and_still_unbounded():
    """No limit means no truncation — the old default, kept so the regression is
    visible rather than assumed."""
    original = _transcript(50)
    assert window_transcript(original) == original


def test_request_count_window_keeps_the_most_recent_entries():
    windowed = window_transcript(_transcript(50), conversation_limit=5)
    assert "step 49" in windowed
    assert "step 46" in windowed
    assert "step 20" not in windowed


def test_the_original_task_instruction_survives_truncation():
    """The head is kept deliberately. Blind head-truncation leaves an agent that
    no longer knows what it was asked to do — a worse failure than forgetting a
    middle step."""
    windowed = window_transcript(_transcript(50), conversation_limit=5)
    assert ">task</agent_request>" in windowed
    assert ELISION in windowed


def test_truncation_is_tail_preserving_so_completed_actions_are_not_forgotten():
    """The stated risk of truncating instead of summarising is that an agent
    forgets a completed action and repeats it — re-sending a real email. Keeping
    the tail is what bounds that risk to old actions only."""
    lines = _transcript(30).split("\n")
    lines.append('<agent_action timestamp="2026-07-21 10:00:00">Sent email to keith</agent_action>')
    windowed = window_transcript("\n".join(lines), conversation_limit=3)
    assert "Sent email to keith" in windowed


def test_char_budget_is_a_hard_ceiling():
    for n in (1, 10, 500):
        windowed = window_transcript(_transcript(n), conversation_limit=20, char_budget=4000)
        assert len(windowed) <= 4000, (n, len(windowed))


def test_growth_is_capped_rather_than_linear():
    """The actual property: prompt size stops tracking history size.

    Without a limit this is a straight line — 10x the history is 10x the prompt.
    """
    small = window_transcript(_transcript(20), conversation_limit=10, char_budget=24000)
    huge = window_transcript(_transcript(2000), conversation_limit=10, char_budget=24000)
    assert len(huge) <= 24000
    # 100x the history, and the prompt did not grow proportionally.
    assert len(huge) < len(small) * 3


def test_a_trigger_agent_firing_every_five_minutes_for_three_days_stays_bounded():
    """864 fires — the exact scenario from plan.md's $170/day fuse."""
    transcript = _transcript(864, payload_size=800)
    assert len(transcript) > 800_000  # ~800KB: the unbounded prompt, for scale
    windowed = window_transcript(transcript, conversation_limit=20, char_budget=24000)
    assert len(windowed) <= 24000


def test_truncation_never_leaves_a_half_open_tag():
    """Entries are dropped at entry boundaries, not character boundaries, so the
    model never receives a fragment of XML it has to guess at."""
    windowed = window_transcript(_transcript(200), conversation_limit=20, char_budget=8000)
    body = windowed.replace(ELISION, "")
    for line in body.split("\n"):
        if line.startswith("<"):
            assert line.endswith(">"), line


def test_runtime_passes_a_limit_so_the_default_is_no_longer_none(monkeypatch: pytest.MonkeyPatch):
    """The one-line regression that reopens the fuse: constructing
    ``ExecutionAgent(name)`` with no limit at ``runtime.py:33``."""
    from server.agents.execution_agent.runtime import ExecutionAgentRuntime
    from server.config import get_settings

    runtime = ExecutionAgentRuntime("some agent")
    settings = get_settings()
    assert runtime.agent.conversation_limit == settings.execution_agent_conversation_limit
    assert runtime.agent.conversation_limit is not None
    assert runtime.agent.history_char_budget == settings.execution_agent_history_char_budget


def test_the_system_prompt_itself_is_bounded(monkeypatch: pytest.MonkeyPatch):
    """End to end through ``build_system_prompt_with_history``, which is what
    actually goes on the wire."""
    agent = ExecutionAgent("bounded agent", conversation_limit=20, history_char_budget=24000)

    class _Store:
        def load_transcript(self, name):
            return _transcript(864, payload_size=800)

    monkeypatch.setattr(agent, "_log_store", _Store())
    prompt = agent.build_system_prompt_with_history()
    base = agent.build_system_prompt()
    assert len(prompt) - len(base) <= 24000 + 64  # + the "# Execution History" header
