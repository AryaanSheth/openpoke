"""Execution Agent implementation."""

from pathlib import Path
from typing import Dict, List, Optional

from ...services.execution import get_execution_agent_logs

# Load system prompt template from file
_prompt_path = Path(__file__).parent / "system_prompt.md"
if _prompt_path.exists():
    SYSTEM_PROMPT_TEMPLATE = _prompt_path.read_text(encoding="utf-8").strip()
else:
    # Placeholder template - you'll replace this with actual instructions
    SYSTEM_PROMPT_TEMPLATE = """You are an execution agent responsible for completing specific tasks using available tools.

Agent Name: {agent_name}
Purpose: {agent_purpose}

Instructions:
[TO BE FILLED IN BY USER]

You have access to Gmail tools to help complete your tasks. When given instructions:
1. Analyze what needs to be done
2. Use the appropriate tools to complete the task
3. Provide clear status updates on your actions

Be thorough, accurate, and efficient in your execution."""


ELISION = "<history_elided>Older history omitted to bound prompt size.</history_elided>"


def _entry_start_indices(lines: List[str]) -> List[int]:
    """Line indices where a transcript entry begins.

    ``render_entry`` (``repositories/formatting.py:66``) writes one entry per
    line, but payloads are stored raw and may contain newlines — so an entry can
    span several lines and only the first one starts with ``<``.
    """
    return [i for i, line in enumerate(lines) if line.startswith("<")]


def window_transcript(
    transcript: str,
    *,
    conversation_limit: Optional[int] = None,
    char_budget: Optional[int] = None,
) -> str:
    """Bound a per-agent transcript, tail-preserving.

    **Why this exists.** ``ExecutionAgentRuntime`` built ``ExecutionAgent(name)``
    with no limit, so ``conversation_limit`` defaulted to ``None`` and the entire
    per-agent log went into the system prompt on every call. A trigger agent
    firing every 5 minutes appends ~1KB per fire; by day 3 its system prompt is
    ~200k tokens, and shortly after that it exceeds the context window and
    **every call fails permanently**. A fuse, not a curve.

    **Why tail-preserving, and why the head is kept anyway.** Truncation makes an
    agent forget what it has already done, and an agent that has forgotten a
    completed action can repeat it — re-sending a real email. Keeping the tail
    preserves recent actions, which is where that risk concentrates. The *first*
    request is kept regardless, because it is the original task instruction: drop
    it and the agent no longer knows what it was asked to do, which is a worse
    failure than forgetting a middle step.

    # ponytail: truncation caps prompt growth; summarize agent history if recall
    # suffers.
    """
    lines = transcript.split("\n")
    starts = _entry_start_indices(lines)
    requests = [i for i in starts if lines[i].startswith("<agent_request")]

    head_lines: List[str] = []
    tail_start = 0

    # 1. Window by request count, keeping the first request (the original task).
    if conversation_limit and conversation_limit > 0 and len(requests) > conversation_limit:
        tail_start = requests[-conversation_limit]
        first = requests[0]
        following = [i for i in starts if i > first]
        head_lines = lines[first : (following[0] if following else len(lines))]

    # 2. Then drop whole entries off the front of the tail until it fits the
    #    budget. Entry boundaries, not characters, so a truncated prompt never
    #    contains half an XML tag.
    if char_budget and char_budget > 0:
        overhead = sum(len(line) + 1 for line in head_lines) + len(ELISION) + 1
        for start in starts:
            if start < tail_start:
                continue
            if overhead + sum(len(line) + 1 for line in lines[start:]) <= char_budget:
                tail_start = start
                break
        else:
            tail_start = starts[-1] if starts else 0

    head_text = "\n".join([*head_lines, ELISION]) if tail_start > 0 else ""
    tail_text = "\n".join(lines[tail_start:])

    # 3. Hard clamp, so the budget is a guarantee rather than a best effort.
    #    Only the tail is clamped: losing the original task instruction is the
    #    one truncation that actually changes what the agent believes it is
    #    doing. If a *single* entry exceeds the whole budget the head still wins
    #    — pick a budget larger than one realistic entry.
    if char_budget and char_budget > 0:
        room = max(char_budget - len(head_text) - 1, 0)
        if len(tail_text) > room:
            tail_text = tail_text[-room:]

    return "\n".join(part for part in (head_text, tail_text) if part).strip()


class ExecutionAgent:
    """Manages state and history for an execution agent."""

    # Initialize execution agent with name, conversation limits, and log store access
    def __init__(
        self,
        name: str,
        conversation_limit: Optional[int] = None,
        history_char_budget: Optional[int] = None,
    ):
        """
        Initialize an execution agent.

        Args:
            name: Human-readable agent name (e.g., 'conversation with keith')
            conversation_limit: Max recent agent requests to keep (None = all — unbounded)
            history_char_budget: Hard ceiling on the rendered history block
        """
        self.name = name
        self.conversation_limit = conversation_limit
        self.history_char_budget = history_char_budget
        self._log_store = get_execution_agent_logs()

    # Generate system prompt template with agent name and purpose derived from name
    def build_system_prompt(self) -> str:
        """Build the system prompt for this agent."""
        agent_purpose = f"Handle tasks related to: {self.name}"

        return SYSTEM_PROMPT_TEMPLATE.format(
            agent_name=self.name,
            agent_purpose=agent_purpose
        )

    # Combine base system prompt with conversation history, applying conversation limits
    def build_system_prompt_with_history(self) -> str:
        """
        Build system prompt including agent history.

        Returns:
            System prompt with embedded history transcript
        """
        base_prompt = self.build_system_prompt()

        transcript = self._log_store.load_transcript(self.name)
        if not transcript:
            return base_prompt

        transcript = window_transcript(
            transcript,
            conversation_limit=self.conversation_limit,
            char_budget=self.history_char_budget,
        )
        return f"{base_prompt}\n\n# Execution History\n\n{transcript}"

    # Format current instruction as user message for LLM consumption
    def build_messages_for_llm(self, current_instruction: str) -> List[Dict[str, str]]:
        """
        Build message array for LLM call.

        Args:
            current_instruction: Current instruction from interaction agent

        Returns:
            List of messages in OpenRouter format
        """
        return [
            {"role": "user", "content": current_instruction}
        ]

    # Log the agent's final response to the execution log store
    def record_response(self, response: str) -> None:
        """Record agent's response to the log."""
        self._log_store.record_agent_response(self.name, response)

    # Log tool invocation and results with truncated content for readability
    def record_tool_execution(self, tool_name: str, arguments: str, result: str) -> None:
        """Record tool execution details."""
        self._log_store.record_action(self.name, f"Calling {tool_name} with: {arguments[:200]}")
        # Record the tool response
        self._log_store.record_tool_response(self.name, tool_name, result[:500])
