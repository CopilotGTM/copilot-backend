## app_layer/utils/socket_app.py
import socketio

# Initialize the Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    credential_required=True,
    ping_interval=240,
    ping_timeout=240,
)
