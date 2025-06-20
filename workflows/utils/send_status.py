from typing import List
from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
import asyncio

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"


class SendStatus:
    @classmethod
    async def send_chat_status(cls, self, status:str, status_message:str = "", sources: List = [], status_type="general_status"):
        if self.sio:
            await self.sio.emit(
                "chat_status_response",
                {   
                    "id": self.system_message_id,
                    "status": status,
                    "status_message": status_message,
                    "sources": sources,
                    "type": status_type,
                },
                room=self.room,
                namespace=WEBSOCKET_NAMESPACE,
            )
            await asyncio.sleep(0)
