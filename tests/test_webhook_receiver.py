"""agent/webhook_receiver.py: real local HTTP server, no docker/network mocking needed --
it only ever talks to 127.0.0.1, so these run unconditionally."""

from __future__ import annotations

import json
import urllib.request

from agent.webhook_receiver import WebhookReceiver


def test_records_method_path_headers_and_body():
    with WebhookReceiver(host="127.0.0.1") as receiver:
        req = urllib.request.Request(
            f"http://127.0.0.1:{receiver.port}/hook/123",
            data=json.dumps({"event": "fired"}).encode("utf-8"),
            headers={"X-Custom": "abc", "Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5.0)

        [received] = receiver.requests
        assert received.method == "POST"
        assert received.path == "/hook/123"
        assert received.headers["X-Custom"] == "abc"
        assert received.json() == {"event": "fired"}


def test_responds_with_configured_status_code():
    with WebhookReceiver(host="127.0.0.1", status_code=204) as receiver:
        req = urllib.request.Request(f"http://127.0.0.1:{receiver.port}/", method="GET")
        response = urllib.request.urlopen(req, timeout=5.0)
        assert response.status == 204


def test_clear_removes_recorded_requests():
    with WebhookReceiver(host="127.0.0.1") as receiver:
        urllib.request.urlopen(f"http://127.0.0.1:{receiver.port}/", timeout=5.0)
        assert len(receiver.requests) == 1
        receiver.clear()
        assert receiver.requests == []


def test_wait_for_request_returns_none_on_timeout_without_blocking_long():
    with WebhookReceiver(host="127.0.0.1") as receiver:
        assert receiver.wait_for_request(timeout=0.5, poll_interval=0.1) is None


def test_wait_for_request_finds_a_request_matching_predicate():
    with WebhookReceiver(host="127.0.0.1") as receiver:
        urllib.request.urlopen(f"http://127.0.0.1:{receiver.port}/other", timeout=5.0)
        urllib.request.urlopen(f"http://127.0.0.1:{receiver.port}/expected", timeout=5.0)

        found = receiver.wait_for_request(lambda r: r.path == "/expected", timeout=5.0, poll_interval=0.1)
        assert found is not None
        assert found.path == "/expected"


def test_port_zero_binds_an_ephemeral_port():
    with WebhookReceiver(host="127.0.0.1", port=0) as receiver:
        assert receiver.port != 0
