# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import logging
import os
from datetime import datetime
from pythonjsonlogger import jsonlogger

# Create a basic, unconfigured logger instance at the module level.
# This logger can be imported by config.py without causing a circular dependency.
logger = logging.getLogger("jb-ai-orchestrator-service")
logger.setLevel(logging.INFO)
# Add a basic handler to see early messages and prevent "no handler" errors.
logger.addHandler(logging.StreamHandler())

class CustomJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        # Import config here to avoid circular dependency at module level
        from app.config import Config
        super(CustomJsonFormatter, self).add_fields(log_record, record, message_dict)
        log_record['DateTime'] = datetime.utcnow().isoformat()
        log_record['Level'] = record.levelname
        log_record['severity'] = record.levelname
        log_record['Logger'] = record.name
        log_record['Region'] = Config.REGION
        log_record['Environment'] = Config.ENVIRONMENT
        log_record['ServiceName'] = Config.SERVICE_NAME
        log_record['Service'] = Config.SERVICE_NAME
        if 'message' in log_record:
            log_record['Message'] = log_record['message']
            del log_record['message']
        if 'exc_info' in log_record:
            log_record['Exception'] = self.formatException(record.exc_info)
            del log_record['exc_info']
        if 'exc_text' in log_record:
            del log_record['exc_text']

def initialize_logger():
    """
    Re-configures the existing logger instance with JSON handlers
    and settings from the Config object.
    """
    # Import config here to avoid circular dependency at module level
    from app.config import Config

    # We are reconfiguring the global logger object
    log_level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(log_level)

    # Clear any existing handlers (like the initial StreamHandler)
    logger.handlers.clear()

    # Create logs directory if it doesn't exist
    log_folder = Config.LOG_FOLDER
    if not os.path.exists(log_folder):
        os.makedirs(log_folder)

    # Create file handler
    log_file = os.path.join(
        log_folder,
        f'{Config.SERVICE_NAME.lower()}.json'
    )
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(log_level)

    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)

    # Use the custom formatter
    formatter = CustomJsonFormatter()
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
