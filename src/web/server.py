"""
uvicorn server start/stop helper.

Called from a Qt6 background thread; runs uvicorn with its own asyncio event loop
in a separate thread so the main thread (Qt6 UI) is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import Any

import uvicorn

logger = logging.getLogger(__name__)


def get_lan_ip() -> str:
    """
    Return the LAN IP address.

    Connects a UDP socket toward an external address so the OS selects the
    outbound interface. No data is actually sent.
    Returns "127.0.0.1" when not connected to any network.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"


class WebServer:
    """
    Helper class that starts and stops uvicorn in a background thread.

    Use from a Qt6 QThread or a plain threading.Thread.

    Usage::

        server = WebServer(app, port=8080)
        url = server.start()  # -> "http://192.168.1.10:8080"
        # ... use the app ...
        server.stop()
    """

    def __init__(
        self,
        app: Any,
        host: str = "0.0.0.0",
        port: int = 8080,
        log_level: str = "warning",
    ) -> None:
        """
        Args:
            app:       FastAPI (ASGI) application
            host:      Bind address (default all interfaces)
            port:      Port number (default 8080)
            log_level: uvicorn log level
        """
        self._app = app
        self._host = host
        self._port = port
        self._log_level = log_level
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def start(self) -> str:
        """
        Start the server in a background thread.

        Returns:
            LAN access URL (e.g. "http://192.168.1.10:8080")
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("WebServer.start() called while already running — ignored")
            return self._access_url()

        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level=self._log_level,
            # A windowed (no-console) build has no sys.stdout, which uvicorn's default
            # log formatter calls isatty() on; the app's own logging handles the output.
            log_config=None,
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)

        self._thread = threading.Thread(
            target=self._run,
            name="uvicorn-web-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("WebServer: started on %s", self._access_url())
        return self._access_url()

    def stop(self, timeout: float = 5.0) -> None:
        """
        Stop the server and wait for the thread to finish.

        Args:
            timeout: Seconds to wait for the thread to exit
        """
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._server = None
        logger.info("WebServer: stopped")

    @property
    def is_running(self) -> bool:
        """Whether the server is currently running."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        """Thread entry point. Creates an asyncio loop and runs uvicorn."""
        assert self._server is not None
        asyncio.run(self._server.serve())

    def _access_url(self) -> str:
        ip = get_lan_ip()
        return f"http://{ip}:{self._port}"
