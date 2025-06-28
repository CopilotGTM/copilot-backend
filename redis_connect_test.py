import os
from redis import StrictRedis
from dotenv import load_dotenv

def main():
    # Load variables from .env file (default: .env in current dir)
    dotenv_path = '.env'  # you can change this to your env file name as needed
    if os.path.exists(dotenv_path):
        print(f"Loading env vars from {dotenv_path}")
        load_dotenv(dotenv_path)
    else:
        print(f"No {dotenv_path} found, using current environment variables")

    REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
    REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
    REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", None)
    REDIS_SSL = os.environ.get("REDIS_SSL", "false").lower() == "true"

    print(f"Connecting to Redis at {REDIS_HOST}:{REDIS_PORT}, db={REDIS_DB}, ssl={REDIS_SSL}")

    try:
        r = StrictRedis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD,
            ssl=True
        )
        pong = r.ping()
        print("Connected! PING:", pong)
    except Exception as e:
        print("Failed to connect to Redis:", e)

if __name__ == "__main__":
    main()