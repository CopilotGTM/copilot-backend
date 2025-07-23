import logging
import os
from typing import Optional


def get_logger(
    logger_name: str = "auth_logger",
    log_level: int = logging.INFO,
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    log_file: Optional[str] = None,
    rotate_logs: bool = False,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Configure and return a logger with customizable settings.
    
    Args:
        logger_name (str): Name of the logger
        log_level (int): Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format (str): Format string for log messages
        log_file (Optional[str]): Path to log file. If None, logs to console only
        rotate_logs (bool): Whether to use rotating file handler
        max_bytes (int): Maximum size of log file before rotation
        backup_count (int): Number of backup files to keep
    
    Returns:
        logging.Logger: Configured logger instance
    """
    logger = logging.getLogger(logger_name)
    
    # Clear existing handlers to prevent duplicates
    if logger.hasHandlers():
        logger.handlers.clear()
    
    logger.setLevel(log_level)
    formatter = logging.Formatter(log_format)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (if log_file is specified)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        if rotate_logs:
            from logging.handlers import RotatingFileHandler
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count
            )
        else:
            file_handler = logging.FileHandler(log_file)
        
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
