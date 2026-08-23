"""Generic HTTP receiver for testing a product's outbound webhook/callback integrations.

Any product a lab tests may have a feature that POSTs (or otherwise calls) an
operator-configured URL when something happens -- a notification hook, a
downstream integration, a callback. Verifying that end-to-end needs something
to point the product at and then ask "what did you actually send me?" This
receiver is that something: a small local HTTP server that records every
request it gets, with no knowledge of any particular product's payload shape.

Not a plugin -- there's no per-product behavior to abstract over, so this is
a concrete utility class in the same vein as agent/container.py's functions,
not an ABC under agent/*/plugin.py.

Usage from a product lab's test suite:

    from agent.webhook_receiver import WebhookReceiver

    with WebhookReceiver() as receiver:
        configure_product_webhook(f"http://{gateway_ip}:{receiver.port}/hook")
        trigger_the_thing_that_should_fire_it()
        request = receiver.wait_for_request(timeout=30.0)
        assert request is not None
        assert request.json()["event"] == "expected-event"

The receiver binds 0.0.0.0 by default so a containerized product can reach it
via the lab's Docker gateway IP (agent.container.get_docker_gateway()) while
the test process itself talks to it over 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


@dataclass
class ReceivedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    received_at: float

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class WebhookReceiver:
    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 0,
        status_code: int = 200,
        response_body: bytes = b"",
    ) -> None:
        self.host = host
        self._status_code = status_code
        self._response_body = response_body
        self._requests: list[ReceivedRequest] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer((host, port), self._make_handler())
        self._thread: threading.Thread | None = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length else b""
                record = ReceivedRequest(
                    method=self.command,
                    path=self.path,
                    headers={k: v for k, v in self.headers.items()},
                    body=body,
                    received_at=time.time(),
                )
                with receiver._lock:
                    receiver._requests.append(record)
                self.send_response(receiver._status_code)
                self.end_headers()
                if receiver._response_body:
                    self.wfile.write(receiver._response_body)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_PATCH = _handle
            do_DELETE = _handle

            def log_message(self, format: str, *args: Any) -> None:
                pass  # requests are readable via receiver.requests; no stderr noise

        return _Handler

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def requests(self) -> list[ReceivedRequest]:
        with self._lock:
            return list(self._requests)

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()

    def start(self) -> "WebhookReceiver":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> "WebhookReceiver":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def wait_for_request(
        self,
        predicate: Callable[[ReceivedRequest], bool] | None = None,
        *,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> ReceivedRequest | None:
        """Poll until a request matching `predicate` has arrived, or timeout elapses."""
        deadline = time.monotonic() + timeout
        while True:
            for record in self.requests:
                if predicate is None or predicate(record):
                    return record
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval)
