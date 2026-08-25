"""Structured JSON logging + correlation-ID propagation (TRD §22.1).

Every log line carries service/environment/version/correlation_id/trace fields and never
a raw secret, token, or camera credential (TRD-SEC-005). Application code should log via
`get_logger(__name__)` and pass `correlation_id=` explicitly rather than relying on a
thread-local, since request handling is async.
"""
from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger


def configure_logging(service_name: str, environment: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "severity"},
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    logging.getLogger("csense").info(
        "logging_configured", extra={"service": service_name, "environment": environment}
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
