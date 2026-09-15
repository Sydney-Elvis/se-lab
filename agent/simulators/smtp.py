"""Reusable Mailpit-backed SMTP fixture and protocol-only test client.

Same shape and rationale as agent.simulators.matrix: any product lab whose
product sends outbound email needs the same two things to test that for
real -- a disposable SMTP catcher to send to, and a way to independently
verify what actually arrived (not just that the app *claims* success).
Mailpit (https://mailpit.axllent.org/) is a small, purpose-built SMTP test
catcher with its own JSON HTTP API for exactly that second half -- MailpitClient
below is a direct lift of family-librarian-lab's own original MailpitClient
(clients.py), which was already 100% Mailpit-protocol-only with zero
Family Librarian knowledge in it; only its location changes.

    from agent.simulators.smtp import MailpitFixture, MailpitClient

    with MailpitFixture(port=18025, smtp_port=11025) as mailpit:
        # ... configure the product under test with (mailpit's internal
        # container address or, from another container, the docker gateway
        # IP -- see agent.container.get_docker_gateway) on smtp_port, AUTH
        # username/password from mailpit.auth_username/auth_password, and
        # (if enable_tls) STARTTLS trusting mailpit.ca_cert_path.
        client = MailpitClient(mailpit.public_host)
        message = client.find_message(to="someone@example.test")

Two things carried over deliberately from the original, rather than
reinvented:

- The SMTP AUTH credential is a fixed username/password with a
  pre-generated bcrypt hash committed as a constant
  (DEFAULT_SMTP_AUTH_FILE_CONTENT), the same choice
  family-librarian-lab's own docker/mailpit/smtp-auth-file already made --
  no bcrypt library is a dependency of this lab, and Mailpit's
  MP_SMTP_AUTH_FILE format requires one. A consumer that genuinely needs
  different credentials should use allow_insecure_auth plus its own
  MailpitFixture subclass overriding docker_volumes(), not a constructor
  parameter this class can't actually honor correctly.
- TLS (enable_tls=True) shells out to `openssl`, matching
  family-librarian-lab's own ensure_smtp_fixture_tls() -- already an
  established assumption in this lab ecosystem, not a new one. The
  generated CA cert path is returned via ca_cert_path so a caller can wire
  it into whatever trust store the product under test reads (an
  SSL_CERT_FILE bundle, an app-level trusted-CA setting, etc.) -- that part
  stays entirely the caller's concern, same as this fixture never assumes
  how the product under test resolves its own hostname.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import common as lab_common
from .base import ExternalSimulator

DEFAULT_SMTP_AUTH_USERNAME = "labmailer"
DEFAULT_SMTP_AUTH_PASSWORD = "Admin123!"
# bcrypt hash of DEFAULT_SMTP_AUTH_PASSWORD for DEFAULT_SMTP_AUTH_USERNAME,
# in Mailpit's MP_SMTP_AUTH_FILE format (htpasswd-style bcrypt). Generated
# once; not derived at runtime -- see the module docstring for why.
DEFAULT_SMTP_AUTH_FILE_CONTENT = "labmailer:$2y$05$sGxV70/ZHwBhhowBLyEaHuQ8QpXsS7NIDBjqyVkWeKFrar1jsSxMm\n"


class MailpitFixture(ExternalSimulator):
    engine_env_var = "SE_LAB_SMTP_ENGINE_DIR"  # unused -- docker-only, see local_command()
    backend_env_var = "SE_LAB_SMTP_BACKEND"
    image_env_var = "SE_LAB_SMTP_IMAGE"
    default_image = "axllent/mailpit:latest"
    container_name_prefix = "se-lab-smtp"
    docker_label_prefix = "com.se-lab.smtp"
    process_marker = "mailpit"  # never actually launched locally; see local_command()
    # No engine checkout exists to build from, ever -- default_image is a
    # pull target, not a build target.
    builds_from_source = False

    # Mailpit's own HTTP API's readiness path -- the same check its
    # container healthcheck already uses (`mailpit readyz`), reachable over
    # plain HTTP too.
    health_check_path = "/readyz"
    reset_path = None  # use clear() on MailpitClient instead -- see its own docstring

    def __init__(
        self,
        *,
        smtp_port: int,
        enable_tls: bool = False,
        tls_common_name: str = "localhost",
        allow_insecure_auth: bool = False,
        tls_dir: Path | None = None,
        **kwargs: Any,
    ) -> None:
        """port (inherited) is Mailpit's HTTP API/UI port -- what
        wait_healthy()/MailpitClient talk to. smtp_port is separate and
        mandatory: the two are never the same port on a real Mailpit
        instance, unlike every single-port fixture ExternalSimulator has
        wrapped so far.

        tls_common_name should match whatever hostname/IP the product under
        test will actually connect with -- TLS hostname validation checks
        against it, not against the address this test process happens to
        reach the fixture on.
        """
        kwargs.setdefault("backend", "docker")
        super().__init__(**kwargs)
        if self.backend != "docker":
            raise ValueError("MailpitFixture only supports backend='docker' -- there is no local engine.")
        self.smtp_port = smtp_port
        self.enable_tls = enable_tls
        self.tls_common_name = tls_common_name
        self.allow_insecure_auth = allow_insecure_auth
        self.auth_username = DEFAULT_SMTP_AUTH_USERNAME
        self.auth_password = DEFAULT_SMTP_AUTH_PASSWORD
        self._tls_dir = tls_dir or (lab_common.runtime_dir() / "se-lab-smtp-tls" / self.container_name)
        self._auth_file_path = self._tls_dir / "smtp-auth-file"
        self.ca_cert_path: Path | None = None

    def local_command(self, engine_dir: Path) -> list[str]:
        raise NotImplementedError(
            "MailpitFixture has no local backend -- it wraps an unmodified third-party "
            "image (Mailpit); pass backend='docker'."
        )

    def docker_run_args(self, image: str) -> list[str]:
        return []

    def additional_ports(self) -> list[int]:
        return [self.smtp_port]

    def docker_env(self) -> dict[str, str]:
        env = {
            "MP_SMTP_BIND_ADDR": f"0.0.0.0:{self.smtp_port}",
            "MP_UI_BIND_ADDR": f"0.0.0.0:{self.port}",
            "MP_SMTP_AUTH_FILE": "/data/smtp-auth-file",
        }
        if self.allow_insecure_auth:
            env["MP_SMTP_AUTH_ALLOW_INSECURE"] = "true"
        if self.enable_tls:
            env["MP_SMTP_TLS_CERT"] = "/data/server.pem"
            env["MP_SMTP_TLS_KEY"] = "/data/server.key"
        return env

    def docker_volumes(self) -> dict[str, str]:
        self._tls_dir.mkdir(parents=True, exist_ok=True)
        self._auth_file_path.write_text(DEFAULT_SMTP_AUTH_FILE_CONTENT, encoding="utf-8")
        volumes = {str(self._auth_file_path.resolve()): "/data/smtp-auth-file"}
        if self.enable_tls:
            server_key, server_cert, ca_cert = self._ensure_tls_files()
            volumes[str(server_key.resolve())] = "/data/server.key"
            volumes[str(server_cert.resolve())] = "/data/server.pem"
            self.ca_cert_path = ca_cert
        return volumes

    def _ensure_tls_files(self) -> tuple[Path, Path, Path]:
        """Self-signed CA + server cert for tls_common_name, cached on disk
        and only regenerated if missing -- matches
        family-librarian-lab's own ensure_smtp_fixture_tls(), the
        established "shell out to openssl" assumption in this lab
        ecosystem (no cryptography-library dependency added for this).
        """
        ca_key = self._tls_dir / "ca.key"
        ca_cert = self._tls_dir / "ca.pem"
        server_key = self._tls_dir / "server.key"
        server_cert = self._tls_dir / "server.pem"

        if not ca_cert.exists():
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                    "-keyout", str(ca_key), "-out", str(ca_cert),
                    "-subj", "/CN=se-lab-smtp-fixture-ca",
                    # Confirmed live: Python's ssl module (unlike the more
                    # lenient validator family-librarian-lab's own
                    # ensure_smtp_fixture_tls() happened to be tested
                    # against) rejects a CA cert with no keyUsage extension
                    # at all -- "CA cert does not include key usage
                    # extension". A real self-signed CA needs this stated
                    # explicitly, not left to defaults.
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                ],
                check=True, capture_output=True,
            )

        if not server_cert.exists():
            server_csr = self._tls_dir / "server.csr"
            ext_file = self._tls_dir / "server.ext"
            ext_file.write_text(f"basicConstraints=CA:FALSE\nsubjectAltName=DNS:{self.tls_common_name}\n")
            subprocess.run(
                [
                    "openssl", "req", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(server_key), "-out", str(server_csr),
                    "-subj", f"/CN={self.tls_common_name}",
                ],
                check=True, capture_output=True,
            )
            subprocess.run(
                [
                    "openssl", "x509", "-req", "-in", str(server_csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
                    "-CAcreateserial", "-out", str(server_cert), "-days", "3650", "-extfile", str(ext_file),
                ],
                check=True, capture_output=True,
            )

        return server_key, server_cert, ca_cert


class MailpitClient:
    """Drives Mailpit's own HTTP API to independently verify SMTP delivery
    -- 'assert against the real destination, not the product's own claim of
    success' pattern. Mailpit's `Username` field on a stored message is the
    SMTP-AUTH identity that actually authenticated the send, so a caller
    can confirm both that a message arrived AND that it arrived
    authenticated as a specific user, not merely that *some* connection
    reached the catcher. No knowledge of any particular product's message
    shapes.
    """

    def __init__(self, base_url: str, *, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def ready(self) -> bool:
        status, _ = self._http(f"{self.base_url}/api/v1/messages?limit=1")
        return status == 200

    def clear(self) -> None:
        """Delete every stored message -- call at the start of a case so an
        earlier case's leftover mail (Mailpit's own state persists for the
        life of the container) can never be mistaken for this case's
        delivery."""
        self._http(f"{self.base_url}/api/v1/messages", method="DELETE", json_body={})

    def messages(self) -> list[dict[str, Any]]:
        """Return the entire small fixture inbox, failing on HTTP errors or truncation."""
        status, body = self._http(f"{self.base_url}/api/v1/messages?limit=100")
        if status != 200:
            raise AssertionError(f"Mailpit inbox returned HTTP {status}")
        payload = json.loads(body)
        messages = payload.get("messages", [])
        if payload.get("total", len(messages)) != len(messages):
            raise AssertionError("Mailpit fixture inbox exceeded one page")
        return messages

    def find_message(
        self, *, to: str, subject_contains: str | None = None, timeout_seconds: float = 15.0
    ) -> dict[str, Any] | None:
        """Polls Mailpit's message list for one already delivered to `to`
        (and, if given, whose subject contains `subject_contains`). Returns
        the message summary (includes `Username`, the authenticated
        SMTP-AUTH identity) or None if nothing matched within the deadline
        -- callers proving a *negative* should pass a short timeout instead
        of waiting out the full default."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            status, body = self._http(f"{self.base_url}/api/v1/messages")
            if status == 200:
                for summary in json.loads(body).get("messages", []):
                    recipients = [
                        address.get("Address")
                        for address in summary.get("To") or []
                        if isinstance(address, dict)
                    ]
                    subject = summary.get("Subject") or ""
                    if to in recipients and (subject_contains is None or subject_contains in subject):
                        return summary
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    def _http(
        self, url: str, *, method: str = "GET", json_body: object | None = None
    ) -> tuple[int, bytes]:
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except urllib.error.URLError:
            return 0, b""
