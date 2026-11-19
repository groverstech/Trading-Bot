"""Run the Grok Bot Terminal: ``python -m grokbot``."""
import logging

import uvicorn

from .api.app import create_app
from .settings import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .endpoints import EndpointError
    try:
        s = load_settings()
    except EndpointError as e:
        logging.error("Refusing to start: %s", e)
        raise SystemExit(2)
    if s.host not in ("127.0.0.1", "localhost"):
        logging.warning("Binding to %s: put the terminal behind TLS and a firewall.", s.host)
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
