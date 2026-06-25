import logging
import os

def setup_logger(name="PropertyAgentLogger"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # Avoid duplicate handlers if initialized multiple times
    if not logger.handlers:
        # Create console handler with clean formatting
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            '%(asctime)s | [%(levelname)s] | %(message)s', 
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
        
        # Create file handler for an architectural audit trail
        os.makedirs("logs", exist_ok=True)
        file_handler = logging.FileHandler("logs/app_execution.log")
        file_handler.setFormatter(console_formatter)
        logger.addHandler(file_handler)
        
    return logger

# Globally available logger instance
log = setup_logger()