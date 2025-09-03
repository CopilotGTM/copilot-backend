import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from src.domain.models.organisation_tables.chat_message_models import ChatMessage
from src.agents.orchestrator import run_agent_for_chat


async def main():
    # Build a minimal message list (role, content)
    messages = [
        ChatMessage(
            id=uuid4(),
            session_id=uuid4(),
            role="user",
            content={"text": "Hi there!"},
            token_count=4,
        ),
        ChatMessage(
            id=uuid4(),
            session_id=uuid4(),
            role="assistant",
            content={"text": "Hello! How can I help?"},
            token_count=6,
        ),
        ChatMessage(
            id=uuid4(),
            session_id=uuid4(),
            role="user",
            content={"text": "Tell me about Mahatma Gandhi in 1000 words."},
            token_count=7,
        ),
    ]

    print("Starting agent workflow...")
    async for frame in run_agent_for_chat(chat_type="general_chat", messages=messages):
        # Serialize each frame to a JSON string (NDJSON)
        print(frame.model_dump_json())
    print("Workflow completed.")


if __name__ == "__main__":
    asyncio.run(main())
