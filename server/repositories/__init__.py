"""Per-tenant repositories.

Every class here takes ``(session, user_id)`` and every query it issues carries
``WHERE user_id = :user_id``. They replace the eight module-level singletons that
were constructed at import time against hardcoded paths under ``server/data/``.

The two module-level helpers that are deliberately *not* tenant-scoped —
``triggers.fetch_due_all`` and ``gmail.list_connected`` — exist for the
background pollers, which service all tenants by definition. Neither is
reachable from an HTTP route.
"""

from .conversation import ConversationRepository, WorkingMemoryRepository
from .execution import AgentLogRepository, AgentRosterRepository
from .gmail import GmailConnectionRepository, GmailSeenRepository
from .triggers import TriggerRepository
from .users import UserRepository

__all__ = [
    "AgentLogRepository",
    "AgentRosterRepository",
    "ConversationRepository",
    "GmailConnectionRepository",
    "GmailSeenRepository",
    "TriggerRepository",
    "UserRepository",
    "WorkingMemoryRepository",
]
