"""Reusable Matrix homeserver fixture and protocol-only test client.

Any product lab whose product sends outbound notifications over Matrix (a
bot account DMing users) needs the same two things to test that for real:
somewhere to run a disposable homeserver, and a way to act as an ordinary
Matrix user against it -- register, log in, accept an invite, send/receive
messages. Neither of those cares what the product under test does with the
messages; that's the exact "generic mechanism, zero product knowledge" bar
WebhookReceiver already set for a plain HTTP callback recorder. This is the
Matrix-protocol equivalent, just heavier because a real client-server
handshake (not a bare HTTP POST) is involved.

    from agent.simulators.matrix import MatrixHomeserverFixture, MatrixTestClient

    with MatrixHomeserverFixture(port=19100) as homeserver:
        bot = MatrixTestClient(homeserver.public_host, homeserver.server_name)
        bot_user_id, bot_token = bot.register_user("fl-bot", "bot-password")

        human = MatrixTestClient(homeserver.public_host, homeserver.server_name)
        human_user_id, human_token = human.register_user("household-member", "member-password")

        # ... configure the product under test with (homeserver.public_host,
        # bot_user_id, bot_token) the same way its own admin settings UI
        # would be filled in, then drive the product's own flow and use
        # `human` to read/reply to whatever the product's bot sends it.

MatrixHomeserverFixture wraps an unmodified third-party image (Continuwuity
-- https://continuwuity.org/, the actively maintained continuation of
Conduwuit/Conduit, a lean Rust homeserver with no external database
dependency -- chosen over the Synapse reference implementation specifically
for the sub-second cold start a fresh container-per-test-case model needs).
There is no local engine checkout to build from -- local_command() always
raises, and callers must pass backend="docker" explicitly (the base class's
normal "local" default does not apply).

Two things were only found by running a real container, not by reading
docs, and are worth flagging for whoever next touches this file:

- The original Famedly Conduit image (matrixconduit/matrix-conduit:latest)
  has a live bug: after a real join, the joining user's own `/sync` kept
  reporting the room under `rooms.invite` rather than `rooms.join`, even
  though a direct room-state query confirmed the membership was "join"
  server-side. Confirmed with room_version 10 and 12 alike, so it isn't a
  room-version issue. That's what motivated switching to Continuwuity,
  where the identical register/invite/join/sync sequence behaves correctly
  -- see agent/simulators/matrix.py's git history / se-lab's own commit
  message for the live evidence if this ever needs re-litigating.
- Continuwuity refuses `allow_registration=true` alone: the first account
  on a fresh instance must register with a one-time bootstrap token the
  server prints to its own stdout at startup (an anti-abuse default, not a
  bug) -- see read_bootstrap_registration_token(). A second, fixed token
  set via docker_env() (registration_token()) is only accepted for every
  registration *after* that bootstrap account exists.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .base import ExternalSimulator

DEFAULT_SERVER_NAME = "matrixfixture.test"
DEFAULT_REGISTRATION_TOKEN = "se-lab-matrix-fixture-token"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_BOOTSTRAP_TOKEN_PATTERN = re.compile(r"registration token ([A-Za-z0-9]+)")


class MatrixHomeserverFixture(ExternalSimulator):
    engine_env_var = "SE_LAB_MATRIX_ENGINE_DIR"  # unused -- docker-only, see local_command()
    backend_env_var = "SE_LAB_MATRIX_BACKEND"
    image_env_var = "SE_LAB_MATRIX_IMAGE"
    default_image = "ghcr.io/continuwuity/continuwuity:latest"
    container_name_prefix = "se-lab-matrix"
    docker_label_prefix = "com.se-lab.matrix"
    process_marker = "continuwuity"  # never actually launched locally; see local_command()
    # No engine checkout exists to build from, ever -- unlike every other
    # simulator, default_image is a pull target, not a build target.
    builds_from_source = False

    # The standard, unauthenticated Client-Server API endpoint every
    # spec-compliant homeserver implements -- proves the homeserver is
    # actually accepting Matrix API calls, not just that the TCP port is
    # open.
    health_check_path = "/_matrix/client/versions"
    # No general-purpose reset endpoint exists; this fixture follows the
    # same "fresh container per case" convention this lab's other
    # ephemeral fixtures (CWA/ABS/mailpit) already use instead of in-place
    # reset.
    reset_path = None

    def __init__(
        self,
        *,
        server_name: str = DEFAULT_SERVER_NAME,
        registration_token: str = DEFAULT_REGISTRATION_TOKEN,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("backend", "docker")
        super().__init__(**kwargs)
        if self.backend != "docker":
            raise ValueError("MatrixHomeserverFixture only supports backend='docker' -- there is no local engine.")
        self.server_name = server_name
        self.registration_token = registration_token

    def local_command(self, engine_dir: Path) -> list[str]:
        raise NotImplementedError(
            "MatrixHomeserverFixture has no local backend -- it wraps an unmodified "
            "third-party image (Continuwuity); pass backend='docker'."
        )

    def docker_run_args(self, image: str) -> list[str]:
        return []

    def docker_env(self) -> dict[str, str]:
        return {
            "CONTINUWUITY_SERVER_NAME": self.server_name,
            "CONTINUWUITY_ADDRESS": "0.0.0.0",
            "CONTINUWUITY_PORT": str(self.port),
            "CONTINUWUITY_DATABASE_PATH": "/var/lib/continuwuity/",
            "CONTINUWUITY_ALLOW_REGISTRATION": "true",
            "CONTINUWUITY_REGISTRATION_TOKEN": self.registration_token,
            "CONTINUWUITY_ALLOW_FEDERATION": "false",
            "CONTINUWUITY_MAX_REQUEST_SIZE": "20000000",
            # A disposable, network-isolated test fixture has no business
            # phoning home for update checks -- this lab's other fixtures
            # (Gutenberg's local catalog, in particular) already hold the
            # same "no live internet dependency" line.
            "CONTINUWUITY_ALLOW_CHECK_FOR_UPDATES": "false",
        }

    def read_bootstrap_registration_token(self, *, timeout: float = 15.0) -> str:
        """The one-time token Continuwuity prints to stdout at startup,
        required for the very first account on a fresh instance regardless
        of allow_registration/registration_token -- an anti-abuse default,
        not something any config value bypasses. Only that first
        registration needs this; pass self.registration_token (the fixed
        value docker_env() configured) to every registration after it.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["docker", "logs", self.container_name], text=True, capture_output=True, check=False,
            )
            combined = _ANSI_ESCAPE.sub("", result.stdout + result.stderr)
            match = _BOOTSTRAP_TOKEN_PATTERN.search(combined)
            if match:
                return match.group(1)
            time.sleep(0.5)
        raise RuntimeError(
            f"Continuwuity never printed its bootstrap registration token within {timeout}s "
            f"(container {self.container_name})."
        )


class MatrixApiError(RuntimeError):
    """A non-2xx response from the homeserver, with the parsed error body when available."""

    def __init__(self, status: int, errcode: str | None, error: str | None):
        self.status = status
        self.errcode = errcode
        self.error = error
        detail = f"{errcode}: {error}" if errcode else (error or f"HTTP {status}")
        super().__init__(f"Matrix API error ({status}): {detail}")


class MatrixTestClient:
    """A plain Matrix Client-Server API v3 client acting as one ordinary user.

    Deliberately separate from whatever HTTP client the product under test
    uses for its own bot account -- this is the test's stand-in for "a
    person using a Matrix client," used both to create the accounts a
    product's admin settings point at and to observe/reply to whatever
    that product's bot sends. No knowledge of any particular product's
    message shapes or business logic.
    """

    def __init__(self, base_url: str, server_name: str, *, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.server_name = server_name
        self.timeout = timeout
        self.access_token: str | None = None
        self.user_id: str | None = None
        self._next_batch: str | None = None

    # -- registration / login -------------------------------------------------

    def register_user(
        self, username: str, password: str, *, registration_token: str | None = None
    ) -> tuple[str, str]:
        """Register a new account and return (user_id, access_token).

        Handles the standard User-Interactive Auth dance: the first call is
        expected to come back 401 with a session id and the auth stage(s)
        the homeserver requires, which is then resubmitted -- no
        homeserver-specific behavior, this is plain Client-Server API v3.
        Pass registration_token when the homeserver requires
        m.login.registration_token (MatrixHomeserverFixture's Continuwuity
        does, for every account after its own bootstrap one -- see
        read_bootstrap_registration_token()); omitted, this falls back to
        the no-token m.login.dummy stage a more permissive homeserver may
        accept instead.
        """
        body: dict[str, Any] = {"username": username, "password": password, "inhibit_login": False}
        status, raw = self._raw_request("POST", "/_matrix/client/v3/register", body)
        if 200 <= status < 300:
            return raw["user_id"], raw["access_token"]
        if status != 401:
            raise MatrixApiError(status, raw.get("errcode") if isinstance(raw, dict) else None,
                                  raw.get("error") if isinstance(raw, dict) else None)

        session = raw.get("session") if isinstance(raw, dict) else None
        if not session:
            raise MatrixApiError(status, raw.get("errcode") if isinstance(raw, dict) else None,
                                  "Registration requires interactive auth but no session id was returned.")
        if registration_token is not None:
            body["auth"] = {"type": "m.login.registration_token", "token": registration_token, "session": session}
        else:
            body["auth"] = {"type": "m.login.dummy", "session": session}
        response = self._request("POST", "/_matrix/client/v3/register", body)
        return response["user_id"], response["access_token"]

    def login(self, username: str, password: str) -> tuple[str, str]:
        body = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
        }
        response = self._request("POST", "/_matrix/client/v3/login", body)
        self.user_id = response["user_id"]
        self.access_token = response["access_token"]
        return self.user_id, self.access_token

    def use_credentials(self, user_id: str, access_token: str) -> None:
        """Adopt an already-known user id/token (e.g. one minted by register_user())
        instead of registering or logging in again."""
        self.user_id = user_id
        self.access_token = access_token

    def whoami(self) -> str:
        response = self._request("GET", "/_matrix/client/v3/account/whoami")
        return response["user_id"]

    # -- rooms / messaging ------------------------------------------------

    def create_direct_room(self, invite_user_id: str) -> str:
        body = {"invite": [invite_user_id], "is_direct": True, "preset": "trusted_private_chat"}
        response = self._request("POST", "/_matrix/client/v3/createRoom", body)
        return response["room_id"]

    def join_room(self, room_id: str) -> None:
        self._request("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/join")
        # Confirmed live: an incremental sync using a `since` token from
        # before the join can miss messages that were sent while this user
        # was only invited (an invite's own sync state is stripped -- no
        # timeline/message content -- so the client never had a chance to
        # see them yet, and a homeserver is not obligated to backfill them
        # into a *since*-bounded delta the way it would an initial sync).
        # Dropping the cursor forces the next sync() to be a fresh initial
        # sync, which does include recent joined-room history.
        self._next_batch = None

    def send_message(self, room_id: str, text: str) -> str:
        transaction_id = str(int(time.time() * 1000))
        path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/send/m.room.message/{transaction_id}"
        response = self._request("PUT", path, {"msgtype": "m.text", "body": text})
        return response["event_id"]

    def sync(self, *, since: str | None = None, timeout_ms: int = 0) -> dict[str, Any]:
        query = f"?timeout={timeout_ms}"
        if since:
            query += f"&since={urllib.parse.quote(since, safe='')}"
        # The socket read timeout must leave real headroom over the
        # server-side long-poll duration we just asked for -- otherwise the
        # local timeout can fire a moment before the homeserver's own
        # response arrives (confirmed live: with both at ~15s, the request
        # timed out client-side right as Conduit was about to answer).
        # Matches HttpMatrixClient.SyncAsync's own 25s-request/40s-socket
        # split on the product side.
        request_timeout = (timeout_ms / 1000.0) + 10.0
        response = self._request("GET", f"/_matrix/client/v3/sync{query}", timeout=request_timeout)
        self._next_batch = response.get("next_batch")
        return response

    def wait_for_invite(self, *, timeout: float = 30.0) -> str | None:
        """Poll /sync until this user has a pending room invite, returning its room id."""
        return self._poll(timeout, lambda body: next(iter((body.get("rooms") or {}).get("invite") or {}), None))

    def wait_for_message(
        self,
        *,
        room_id: str | None = None,
        predicate: Callable[[str, str], bool] | None = None,
        timeout: float = 30.0,
    ) -> tuple[str, str] | None:
        """Poll /sync until a joined room receives a text message matching
        `predicate(room_id, body)` (default: any message), returning (room_id, body)."""

        def _check(sync_body: dict[str, Any]) -> tuple[str, str] | None:
            joined = (sync_body.get("rooms") or {}).get("join") or {}
            for candidate_room_id, room in joined.items():
                if room_id is not None and candidate_room_id != room_id:
                    continue
                for event in (room.get("timeline") or {}).get("events") or []:
                    if event.get("type") != "m.room.message":
                        continue
                    content = event.get("content") or {}
                    if content.get("msgtype") != "m.text":
                        continue
                    text = content.get("body")
                    if text is None:
                        continue
                    if predicate is None or predicate(candidate_room_id, text):
                        return candidate_room_id, text
            return None

        return self._poll(timeout, _check)

    def _poll(self, timeout: float, check: Callable[[dict[str, Any]], Any]) -> Any:
        deadline = time.monotonic() + timeout
        since = self._next_batch
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            long_poll_ms = int(min(remaining, 20.0) * 1000)
            body = self.sync(since=since, timeout_ms=max(long_poll_ms, 0))
            since = self._next_batch
            result = check(body)
            if result is not None:
                return result

    # -- transport ----------------------------------------------------------

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        status, parsed = self._raw_request(method, path, body, timeout=timeout)
        if status < 200 or status >= 300:
            errcode = parsed.get("errcode") if isinstance(parsed, dict) else None
            error = parsed.get("error") if isinstance(parsed, dict) else None
            raise MatrixApiError(status, errcode, error)
        return parsed if isinstance(parsed, dict) else {}

    def _raw_request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return exc.code, {"error": raw.decode("utf-8", errors="replace")}
