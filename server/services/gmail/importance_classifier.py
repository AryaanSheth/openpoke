"""LLM-powered classifier for determining important Gmail emails.

**Scope expansion, deliberate.** This module used to return ``Optional[str]``
(original ``:102-113``): every failure path returned ``None``, which is the same
value it returns for "not important". The watcher then marked the message id seen
regardless (original ``importance_watcher.py:210``), so **a transient OpenRouter
blip dropped that email permanently.** That is data loss, not a performance
ceiling, so it is fixed here rather than deferred: the return type now
distinguishes *a verdict* from *a failure*, and the watcher only settles the id
when a verdict came back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...config import get_settings
from ...logging_config import logger
from ...openrouter_client import OpenRouterError, request_chat_completion
from .processing import ProcessedEmail

_TOOL_NAME = "mark_email_importance"
_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": (
            "Decide whether an email should be proactively surfaced to the user and, "
            "if so, provide a natural-language summary explaining why."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "important": {
                    "type": "boolean",
                    "description": (
                        "Set to true only when the email requires timely attention, a decision, "
                        "coordination, or contains critical security information (e.g. OTPs)."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "Concise 2-3 sentence summary highlighting sender, topic, and the "
                        "specific action or urgency for the user. Only include when important=true."
                    ),
                },
            },
            "required": ["important"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = (
    "You review incoming Gmail messages and decide whether they warrant an immediate proactive "
    "notification to the user. Only mark an email as important if it materially affects the "
    "user's plans, requires a prompt decision or action, is a security-sensitive OTP or login "
    "notice, or contains high-priority updates (e.g. interviews, meeting changes). Ignore "
    "order confirmations, routine marketing, newsletters, generic receipts, and low-impact "
    "status notifications. When important, craft a brief summary that will be forwarded to the "
    "user describing what happened and why it matters."
)


def _format_email_payload(email: ProcessedEmail) -> str:
    attachments = ", ".join(email.attachment_filenames) if email.attachment_filenames else "None"
    labels = ", ".join(email.label_ids) if email.label_ids else "None"
    header_lines = [
        f"Sender: {email.sender}",
        f"Recipient: {email.recipient}",
        f"Subject: {email.subject}",
        f"Received (user timezone): {email.timestamp.isoformat()}",
        f"Thread ID: {email.thread_id or 'None'}",
        f"Labels: {labels}",
        f"Has attachments: {'Yes' if email.has_attachments else 'No'}",
        f"Attachment filenames: {attachments}",
    ]

    return (
        "Email Metadata:\n"
        + "\n".join(header_lines)
        + "\n\nCleaned Body:\n"
        + (email.clean_text or "(empty body)")
    )


@dataclass(frozen=True)
class Classification:
    """A classifier outcome.

    ``decided`` is the field that matters: ``False`` means we never got an
    answer, so the caller must **not** treat the message as handled.
    """

    decided: bool
    summary: str | None = None
    error: str | None = None

    @property
    def important(self) -> bool:
        return self.decided and bool(self.summary)

    @classmethod
    def failed(cls, error: str) -> Classification:
        return cls(decided=False, error=error)

    @classmethod
    def not_important(cls) -> Classification:
        return cls(decided=True)

    @classmethod
    def surfaced(cls, summary: str) -> Classification:
        return cls(decided=True, summary=summary)


async def classify_email_importance(email: ProcessedEmail) -> Classification:
    """Classify one email. A failure is reported as a failure, not as "boring"."""

    settings = get_settings()
    api_key = settings.openrouter_api_key
    model = settings.email_classifier_model

    if not api_key:
        logger.warning("Skipping importance check; OpenRouter API key missing")
        return Classification.failed("openrouter api key missing")

    user_payload = _format_email_payload(email)
    messages = [{"role": "user", "content": user_payload}]

    try:
        response = await request_chat_completion(
            model=model,
            messages=messages,
            system=_SYSTEM_PROMPT,
            api_key=api_key,
            tools=[_TOOL_SCHEMA],
        )
    except OpenRouterError as exc:
        logger.error(
            "Importance classification failed",
            extra={"message_id": email.id, "error": str(exc)},
        )
        return Classification.failed(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "Unexpected error during importance classification",
            extra={"message_id": email.id},
        )
        return Classification.failed(str(exc))

    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []

    for tool_call in tool_calls:
        function_block = tool_call.get("function") or {}
        if function_block.get("name") != _TOOL_NAME:
            continue

        arguments = _coerce_arguments(function_block.get("arguments"))
        if arguments is None:
            # Malformed output from the model is a *decision* we cannot make,
            # but retrying it is likely to produce the same garbage. Treat it as
            # undecided; the watcher's retry budget bounds the loop.
            logger.warning(
                "Importance tool returned invalid arguments",
                extra={"message_id": email.id},
            )
            return Classification.failed("invalid tool arguments")

        if not bool(arguments.get("important")):
            return Classification.not_important()

        summary = arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            logger.warning(
                "Importance tool marked email important without summary",
                extra={"message_id": email.id},
            )
            return Classification.failed("important without summary")

        return Classification.surfaced(summary.strip())

    logger.debug(
        "Importance classification produced no tool call",
        extra={"message_id": email.id},
    )
    return Classification.failed("no tool call in response")


def _coerce_arguments(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


__all__ = ["Classification", "classify_email_importance"]
