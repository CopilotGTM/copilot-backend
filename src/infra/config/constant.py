import os

CONFIG_MAP = {
    "local": {
        "redis_client": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
            "password": os.getenv("REDIS_PASSWORD", None),
        },
    },
    "dev": {
        "redis_client": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
            "password": os.getenv("REDIS_PASSWORD", None),
        },
    },
    "prod": {
        "redis_client": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
            "password": os.getenv("REDIS_PASSWORD", None),
        },
    },
}

GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
