# app_layer/utils/cursor.py
import base64
import json
from datetime import datetime
from uuid import UUID


def encode_cursor(dt: datetime, uid: UUID) -> str:
    blob = json.dumps({"ts": dt.isoformat(), "id": str(uid)}).encode()
    return base64.urlsafe_b64encode(blob).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    return datetime.fromisoformat(data["ts"]), UUID(data["id"])
