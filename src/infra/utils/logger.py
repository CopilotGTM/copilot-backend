import logging

def get_logger():
    logger = logging.getLogger("auth_logger")
    if not logger.hasHandlers():  # Prevent adding handlers multiple times in FastAPI reloads
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger