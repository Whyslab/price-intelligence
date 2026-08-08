"""
Structured JSON Logger for Price Intelligence.
Supports correlation IDs (run_id) for tracing pipeline runs across subprocesses.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        # Add correlation ID if present in environment or record
        run_id = getattr(record, 'run_id', os.environ.get('PIPELINE_RUN_ID'))
        if run_id:
            log_record["run_id"] = run_id
            
        if record.exc_info and record.exc_info[0]:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record, ensure_ascii=False)

def get_logger(name="price_intelligence"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        
    return logger

# Global logger instance
log = get_logger()
