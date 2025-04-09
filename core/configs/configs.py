import os

from core.configs.constant import CONFIG_MAP


class Config:
    def __init__(self):
        self.env = os.getenv("ENV", "dev")
        self._config = CONFIG_MAP[self.env]
