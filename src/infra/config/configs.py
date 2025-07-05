import os

from .constant import CONFIG_MAP
from ..utils.logger import get_logger

logger = get_logger()


class Config:
    def __init__(self):
        self.env = os.getenv("ENV", "dev")
        self._config = CONFIG_MAP[self.env]
        logger.info(f"Config loaded for environment: {self.env}")

    @property
    def get_redis_client_details(self):
        redis_details = self._config["redis_client"]
        return redis_details["host"], redis_details["port"], redis_details["password"]
