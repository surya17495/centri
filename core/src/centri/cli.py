"""CENTRI CLI entry point."""

import logging

import uvicorn

from centri.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def run() -> None:
    settings = get_settings()
    logger.info("Starting CENTRI on %s:%d", settings.core_host, settings.core_port)
    uvicorn.run(
        "centri.app:app",
        host=settings.core_host,
        port=settings.core_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
