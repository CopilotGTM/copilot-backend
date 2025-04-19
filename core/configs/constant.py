import os

CONFIG_MAP = {
    "local": {
        "redis_client": {"host": os.getenv("REDIS_HOST", "localhost"), "port": int(os.getenv("REDIS_PORT", 6379))},
    },
    "dev": {
        "redis_client": {"host": os.getenv("REDIS_HOST", "localhost"), "port": int(os.getenv("REDIS_PORT", 6379))},
    },
    "prod": {
        "redis_client": {"host": os.getenv("REDIS_HOST", "localhost"), "port": int(os.getenv("REDIS_PORT", 6379))},
    },
}

GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
