"""Start the V2 API:  python -m app.start   (port from config/.env, default 8001)."""

import logging
import sys

import uvicorn

from config.settings import settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print(f"Text Search v2 -> http://{settings.api_host}:{settings.api_port}")
    uvicorn.run("app.api:app", host=settings.api_host, port=settings.api_port, log_level="info",
                loop="app.runtime:selector_loop_factory" if sys.platform == "win32" else "auto")
