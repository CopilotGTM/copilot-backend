## app_layer/utils/socket_app.py
import socketio

# Initialize the Socket.IO server
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
