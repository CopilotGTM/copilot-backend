"""
Agent Context Manager for maintaining conversation state and context across interactions.
Supports multi-turn conversations, context switching, and state persistence.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
from datetime import datetime

from src.domain.models.organisation_tables.chat_message_models import ChatMessage


class ConversationContext:
    """Maintains context for a conversation session."""

    def __init__(self, session_id: UUID, user_id: Optional[UUID] = None):
        self.session_id = session_id
        self.user_id = user_id
        self.context_data: Dict[str, Any] = {}
        self.interaction_history: List[Dict] = []
        self.pending_questions: List[Dict] = []
        self.current_agent: Optional[str] = None
        self.agent_switches: List[Dict] = []
        self.created_at = datetime.now()
        self.last_updated = datetime.now()

    def add_user_input(self, message: ChatMessage):
        """Add user input to context."""
        self.interaction_history.append(
            {
                "type": "user_input",
                "message_id": str(message.message_id),
                "content": message.content,
                "timestamp": datetime.now(),
            }
        )
        self.last_updated = datetime.now()

    def add_agent_response(self, response_summary: Dict):
        """Add agent response summary to context."""
        self.interaction_history.append(
            {
                "type": "agent_response",
                "agent": self.current_agent,
                "summary": response_summary,
                "timestamp": datetime.now(),
            }
        )
        self.last_updated = datetime.now()

    def add_pending_question(self, question_data: Dict):
        """Add a pending question from agent."""
        question_id = str(uuid4())
        question_entry = {
            "id": question_id,
            "prompt": question_data.get("prompt"),
            "fields": question_data.get("fields", []),
            "created_at": datetime.now(),
            "status": "pending",
        }
        self.pending_questions.append(question_entry)
        return question_id

    def answer_question(self, question_id: str, answer: Dict) -> bool:
        """Mark a question as answered."""
        for q in self.pending_questions:
            if q["id"] == question_id:
                q["answer"] = answer
                q["status"] = "answered"
                q["answered_at"] = datetime.now()
                return True
        return False

    def request_agent_switch(self, switch_data: Dict):
        """Record agent switch request."""
        switch_entry = {
            "id": str(uuid4()),
            "from_agent": self.current_agent,
            "to_agent": switch_data.get("to_agent"),
            "reason": switch_data.get("reason"),
            "confidence": switch_data.get("confidence"),
            "requested_at": datetime.now(),
            "status": "pending",
        }
        self.agent_switches.append(switch_entry)
        return switch_entry["id"]

    def execute_agent_switch(self, switch_id: str) -> bool:
        """Execute a pending agent switch."""
        for switch in self.agent_switches:
            if switch["id"] == switch_id and switch["status"] == "pending":
                self.current_agent = switch["to_agent"]
                switch["status"] = "executed"
                switch["executed_at"] = datetime.now()
                return True
        return False

    def set_context_data(self, key: str, value: Any):
        """Set context data."""
        self.context_data[key] = value
        self.last_updated = datetime.now()

    def get_context_data(self, key: str, default: Any = None) -> Any:
        """Get context data."""
        return self.context_data.get(key, default)

    def get_recent_interactions(self, limit: int = 5) -> List[Dict]:
        """Get recent interaction history."""
        return self.interaction_history[-limit:]

    def get_pending_questions(self) -> List[Dict]:
        """Get all pending questions."""
        return [q for q in self.pending_questions if q["status"] == "pending"]

    def get_pending_agent_switches(self) -> List[Dict]:
        """Get pending agent switches."""
        return [s for s in self.agent_switches if s["status"] == "pending"]

    def summarize(self) -> Dict[str, Any]:
        """Get context summary."""
        return {
            "session_id": str(self.session_id),
            "current_agent": self.current_agent,
            "interaction_count": len(self.interaction_history),
            "pending_questions": len(self.get_pending_questions()),
            "pending_switches": len(self.get_pending_agent_switches()),
            "context_keys": list(self.context_data.keys()),
            "last_updated": self.last_updated,
            "duration_minutes": (datetime.now() - self.created_at).total_seconds() / 60,
        }


class ContextManager:
    """Manages conversation contexts across sessions."""

    def __init__(self):
        self._contexts: Dict[UUID, ConversationContext] = {}

    def get_or_create_context(
        self, session_id: UUID, user_id: Optional[UUID] = None
    ) -> ConversationContext:
        """Get existing context or create new one."""
        if session_id not in self._contexts:
            self._contexts[session_id] = ConversationContext(session_id, user_id)
        return self._contexts[session_id]

    def get_context(self, session_id: UUID) -> Optional[ConversationContext]:
        """Get existing context."""
        return self._contexts.get(session_id)

    def remove_context(self, session_id: UUID) -> bool:
        """Remove context."""
        if session_id in self._contexts:
            del self._contexts[session_id]
            return True
        return False

    def cleanup_old_contexts(self, max_age_hours: int = 24):
        """Clean up old contexts."""
        now = datetime.now()
        to_remove = []

        for session_id, context in self._contexts.items():
            age_hours = (now - context.last_updated).total_seconds() / 3600
            if age_hours > max_age_hours:
                to_remove.append(session_id)

        for session_id in to_remove:
            del self._contexts[session_id]

        return len(to_remove)

    def get_all_contexts_summary(self) -> List[Dict]:
        """Get summary of all contexts."""
        return [ctx.summarize() for ctx in self._contexts.values()]


# Global context manager instance
context_manager = ContextManager()
