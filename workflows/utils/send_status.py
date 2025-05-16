from app_layer.utils.socket_app import sio
from app_layer.utils.logger import get_logger
import asyncio

logger = get_logger()

WEBSOCKET_NAMESPACE = "/websocket/v1"


class SendStatus:
    @classmethod
    async def send_chat_status(cls, self, status, status_message, status_type="basic"):
        if self.sio:
            await self.sio.emit(
                "chat_status_response",
                {
                    "status": status,
                    "status_message": status_message,
                    "id": self.system_message_id,
                    "type": status_type,
                },
                room=self.room,
                namespace=WEBSOCKET_NAMESPACE,
            )
            await asyncio.sleep(0)