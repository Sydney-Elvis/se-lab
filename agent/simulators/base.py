"""Base for a lab's own local/docker-backed fake of an external service.

Extracted from m3undle_lab/simulator.py's SimulatorInstance (576 lines),
which mixed generic subprocess/docker lifecycle, stale-port cleanup, and
health polling with M3Undle's own provider-simulator CLI contract throughout
-- the exact same shape of duplication ClientPlugin/DatabasePlugin/
AnalysisPlugin/SettingsPlugin already exist to prevent, just not yet applied
here. Split the same way those were: this base class owns everything that
doesn't care what's being simulated (backend selection, process/container
lifecycle, port-conflict cleanup, health polling); a subclass supplies the
two abstract methods below plus whatever engine-specific naming it needs.

    from agent.simulators.base import ExternalSimulator

    class MyEngineSimulator(ExternalSimulator):
        engine_env_var = "MYPRODUCT_SIMULATOR_ENGINE_DIR"
        backend_env_var = "MYPRODUCT_SIMULATOR_BACKEND"
        image_env_var = "MYPRODUCT_SIMULATOR_IMAGE"
        default_image = "myproduct-lab/engine-sim:dev"
        container_name_prefix = "myproduct-sim"
        docker_label_prefix = "com.myproduct-lab"
        process_marker = "engine_sim.py"  # substring in `ps`/`ss` cmdline output

        def local_command(self, engine_dir: Path) -> list[str]:
            return [sys.executable, str(engine_dir / "src" / "engine_sim.py"),
                    "--port", str(self.port)]

        def docker_run_args(self, image: str) -> list[str]:
            return ["--port", str(self.port)]

Only what's inherently per-engine is abstract: the CLI contract to launch the
engine locally, and the extra `docker run` args for the container backend.
Everything else -- fixture/scenario bind-mounts, port-conflict cleanup,
health polling, docker image build-if-default -- is identical regardless of
what's being simulated, so it lives here once.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import time
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .. import common as lab_common, runtime

VALID_BACKENDS = frozenset({"local", "docker"})


class DockerUnavailableError(RuntimeError):
    """Raised when the docker backend is selected but the CLI can't run."""


def _run_docker(*args: str, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout, check=check)
    except FileNotFoundError as exc:
        raise DockerUnavailableError("Docker backend requested but the `docker` CLI is not available") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"docker {' '.join(args)} failed: {exc.stderr.strip()}") from exc


class ExternalSimulator(ABC):
    # Required subclass configuration -- no defaults, every simulator needs its own.
    engine_env_var: str
    backend_env_var: str
    image_env_var: str
    default_image: str
    container_name_prefix: str
    docker_label_prefix: str
    process_marker: str  # substring identifying this engine's process in `ps`/`ss` output, for stale-port cleanup

    # Optional, generic-enough defaults a subclass can override.
    health_check_path: str = "/health"
    reset_path: str | None = "/debug/reset"
    fixtures_mount_path: str = "/app/fixtures"
    scenarios_mount_path: str = "/app/scenarios"

    FIXTURES_DIR = lab_common.FIXTURES_DIR
    SCENARIOS_DIR = runtime.REPO_ROOT / "scenarios"

    def __init__(
        self,
        *,
        port: int,
        bind: str = "127.0.0.1",
        public_host: str | None = None,
        backend: str | None = None,
        image: str | None = None,
        run_id: str | None = None,
        suite: str | None = None,
    ) -> None:
        """
        bind        -- interface the simulator listens on (default 127.0.0.1).
                      Ignored by the docker backend, which always binds
                      0.0.0.0 inside the container and reaches the host via
                      the published port instead.
        public_host -- base URL a subclass advertises to the thing under
                      test. Defaults to http://{bind}:{port}.
        backend     -- "local" (subprocess) or "docker" (container). Defaults
                      to this class's backend_env_var / lab.env setting, then
                      "local". Callers should not hard-code this -- let it
                      resolve centrally so the same test code runs against
                      either backend.
        image       -- docker backend only. Defaults to default_image, which
                      is rebuilt from the external engine checkout's root
                      Dockerfile (<engine_dir>/Dockerfile) on every start() so
                      the container always reflects current source. Set this
                      to a pinned/pulled tag to skip that rebuild -- or leave
                      it unset and export this class's image_env_var: it
                      builds the image once per run and passes the tag down
                      via that env var, so every instance skips the redundant
                      rebuild instead of each one repeating it.
        run_id, suite -- optional; recorded as container labels
                      (<docker_label_prefix>.run-id / .suite) for the docker
                      backend. Not required.
        """
        self.port = port
        self.bind = bind
        self.public_host = public_host or f"http://{bind}:{port}"

        self.backend = backend or lab_common.resolve_setting(self.backend_env_var, default="local")
        if self.backend not in VALID_BACKENDS:
            raise ValueError(
                f"Unsupported simulator backend {self.backend!r}. Expected one of: {', '.join(sorted(VALID_BACKENDS))}."
            )
        self.image = image or os.environ.get(self.image_env_var) or self.default_image
        self._image_is_default = image is None and not os.environ.get(self.image_env_var)
        self.run_id = run_id
        self.suite = suite
        self.container_name = f"{self.container_name_prefix}-{port}"

        # Local health-check URL -- always reachable from the host regardless
        # of what public_host is set to (e.g. host.docker.internal won't
        # resolve on the host itself), and identical for both backends since
        # the docker backend always publishes the container port to the host.
        _local_bind = "127.0.0.1" if bind == "0.0.0.0" else bind
        self._local_url = f"http://{_local_bind}:{port}"
        self._process: subprocess.Popen | None = None
        self._log_fh = None
        self._log_path: Path | None = None
        self._container_started = False

    def _engine_dir_or_none(self) -> Path | None:
        value = lab_common.resolve_setting(self.engine_env_var)
        return Path(value) if value else None

    def _require_engine_dir(self) -> Path:
        engine_dir = self._engine_dir_or_none()
        if engine_dir is None:
            raise RuntimeError(
                f"{self.engine_env_var} is not set. Point it at a checkout of the simulator engine in lab.env."
            )
        return engine_dir

    # -------------------------------------------------------------------
    # Subclass contract -- the only two things inherently per-engine.
    # -------------------------------------------------------------------

    @abstractmethod
    def local_command(self, engine_dir: Path) -> list[str]:
        """Argv to launch the engine as a local subprocess.

        Include sys.executable explicitly if the engine is a Python script --
        the base class doesn't assume a language.
        """

    @abstractmethod
    def docker_run_args(self, image: str) -> list[str]:
        """Extra args appended after the image name in `docker run`.

        Fixture/scenario bind-mounts and standard labels are already added by
        the base class; use container_path_for() to translate a host path
        under FIXTURES_DIR/SCENARIOS_DIR into its mounted container path for
        whatever flag your engine expects.
        """

    def container_path_for(self, host_path: Path) -> str:
        """Map a host path under FIXTURES_DIR/SCENARIOS_DIR to its path
        inside the simulator container, per the mounts _start_docker() adds."""
        resolved = host_path.resolve()
        for host_root, container_root in (
            (self.FIXTURES_DIR.resolve(), self.fixtures_mount_path),
            (self.SCENARIOS_DIR.resolve(), self.scenarios_mount_path),
        ):
            try:
                rel = resolved.relative_to(host_root)
            except ValueError:
                continue
            return f"{container_root}/{rel.as_posix()}"
        raise RuntimeError(
            f"Docker backend requires paths under {self.FIXTURES_DIR} or {self.SCENARIOS_DIR} "
            f"(got {resolved}); those directories are what get bind-mounted into the container."
        )

    # -------------------------------------------------------------------
    # Port/process helpers (local backend)
    # -------------------------------------------------------------------

    def _port_is_bindable(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((self.bind, self.port))
            return True
        except OSError:
            return False

    def _list_listening_pids(self) -> list[int]:
        """Best-effort PID discovery for listeners on the simulator port."""
        pids: set[int] = set()

        try:
            result = subprocess.run(["ss", "-ltnp"], text=True, capture_output=True, check=False)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if f":{self.port}" not in line:
                        continue
                    for match in re.finditer(r"pid=(\d+)", line):
                        pids.add(int(match.group(1)))
        except FileNotFoundError:
            pass

        if not pids:
            try:
                result = subprocess.run(
                    ["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-t"],
                    text=True, capture_output=True, check=False,
                )
                if result.returncode == 0:
                    for raw in result.stdout.splitlines():
                        raw = raw.strip()
                        if raw.isdigit():
                            pids.add(int(raw))
            except FileNotFoundError:
                pass

        return sorted(pid for pid in pids if pid > 0 and pid != os.getpid())

    @staticmethod
    def _cmdline_for_pid(pid: int) -> str:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return ""
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()

    @staticmethod
    def _terminate_pid(pid: int, *, timeout: float = 5.0) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                break
            time.sleep(0.1)

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return True

    def _cleanup_stale_port_owner(self) -> None:
        """
        Best-effort cleanup:
        - Kill stale simulator listeners (matching process_marker) on this port.
        - Refuse startup if a non-simulator process still owns the port.
        """
        if self._port_is_bindable():
            return

        pids = self._list_listening_pids()
        if not pids:
            return

        stale_sim_pids: list[int] = []
        foreign_pids: list[int] = []
        for pid in pids:
            cmdline = self._cmdline_for_pid(pid)
            if self.process_marker in cmdline:
                stale_sim_pids.append(pid)
            else:
                foreign_pids.append(pid)

        if stale_sim_pids:
            print(
                f"  Port {self.port} already in use by stale simulator PID(s): {stale_sim_pids}. "
                "Attempting cleanup...",
                flush=True,
            )
            for pid in stale_sim_pids:
                self._terminate_pid(pid)
            time.sleep(0.2)

        if self._port_is_bindable():
            return

        remaining = self._list_listening_pids()
        if not remaining:
            return

        remaining_cmd = ", ".join(f"{pid}:{self._cmdline_for_pid(pid) or '<unknown>'}" for pid in remaining)
        if foreign_pids:
            raise RuntimeError(f"Port {self.port} is occupied by non-simulator process(es): {remaining_cmd}")
        raise RuntimeError(f"Port {self.port} still occupied after stale simulator cleanup: {remaining_cmd}")

    # -------------------------------------------------------------------
    # Docker helpers
    # -------------------------------------------------------------------

    def _ensure_image(self) -> None:
        if not self._image_is_default:
            # Caller pinned an explicit image (e.g. a pulled/published tag,
            # or image_env_var signaling a run-level prebuild); treat it as
            # externally managed and never rebuild it.
            return
        self.build_image()

    def build_image(self) -> str:
        """Build self.image from the engine checkout's Dockerfile and return its tag.

        Exposed so a multi-suite runner can build once per invocation and
        export the result via image_env_var, instead of every instance's
        start() repeating the same build.
        """
        engine_dir = self._require_engine_dir()
        dockerfile = engine_dir / "Dockerfile"
        print(f"Building simulator image {self.image} from {dockerfile}...", flush=True)
        _run_docker("build", "-f", str(dockerfile), "-t", self.image, str(engine_dir))
        return self.image

    def _cleanup_stale_container(self) -> None:
        _run_docker("rm", "-f", self.container_name, check=False)

    def _start_docker(self, log_path: Path | None) -> None:
        self._ensure_image()
        self._cleanup_stale_container()

        cmd = [
            "run", "-d",
            "--name", self.container_name,
            "-p", f"{self.port}:{self.port}",
            "-v", f"{self.FIXTURES_DIR.resolve()}:{self.fixtures_mount_path}:ro",
        ]
        if self.SCENARIOS_DIR.is_dir():
            cmd += ["-v", f"{self.SCENARIOS_DIR.resolve()}:{self.scenarios_mount_path}:ro"]
        cmd += [
            "--label", f"{self.docker_label_prefix}.component=simulator",
            "--label", f"{self.docker_label_prefix}.port={self.port}",
        ]
        if self.run_id:
            cmd += ["--label", f"{self.docker_label_prefix}.run-id={self.run_id}"]
        if self.suite:
            cmd += ["--label", f"{self.docker_label_prefix}.suite={self.suite}"]
        cmd += [self.image, *self.docker_run_args(self.image)]

        print(f"  Starting simulator container {self.container_name} on port {self.port}...", flush=True)
        _run_docker(*cmd)
        self._container_started = True
        self._log_path = log_path

    def _stop_docker(self) -> None:
        if not self._container_started:
            return
        print(f"  Stopping simulator container {self.container_name}...", flush=True)
        if self._log_path is not None:
            try:
                logs = _run_docker("logs", self.container_name, check=False)
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_path.write_text(logs.stdout + logs.stderr, encoding="utf-8")
            except DockerUnavailableError:
                pass
        _run_docker("rm", "-f", self.container_name, check=False)
        self._container_started = False

    def _docker_is_running(self) -> bool:
        if not self._container_started:
            return False
        try:
            result = _run_docker("inspect", "--format", "{{.State.Running}}", self.container_name, check=False)
        except DockerUnavailableError:
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def start(self, log_path: Path | None = None) -> None:
        if self.is_running():
            raise RuntimeError(f"Simulator on port {self.port} is already running")

        self._cleanup_stale_port_owner()

        if self.backend == "docker":
            self._start_docker(log_path)
            return

        engine_dir = self._require_engine_dir()
        cmd = self.local_command(engine_dir)

        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(log_path, "w", encoding="utf-8")
            stdout = stderr = self._log_fh
        else:
            stdout = stderr = subprocess.DEVNULL

        print(f"  Starting simulator on port {self.port}...", flush=True)
        # cwd pinned to runtime.REPO_ROOT: a lab-private fixture path may live
        # outside the external engine checkout, so the engine's own
        # cwd-relative fallback resolution must not depend on where the
        # invoking command happened to run from. The docker backend takes a
        # different path -- it mounts FIXTURES_DIR/SCENARIOS_DIR into the
        # container directly and doesn't go through this method.
        self._process = subprocess.Popen(
            cmd,
            cwd=str(runtime.REPO_ROOT),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    def wait_healthy(self, *, timeout: float = 15.0) -> bool:
        """Poll health_check_path until the simulator responds 200 or timeout elapses."""
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if not self.is_running():
                print("  Simulator exited early before becoming healthy", flush=True)
                return False
            try:
                with urllib.request.urlopen(f"{self._local_url}{self.health_check_path}", timeout=2.0) as resp:
                    if resp.status == 200:
                        return True
                    last_error = f"HTTP {resp.status}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)
        print(f"  Simulator health check timed out. Last error: {last_error}", flush=True)
        return False

    def stop(self) -> None:
        if self.backend == "docker":
            self._stop_docker()
            return

        if self._process is None:
            return
        print(f"  Stopping simulator (port {self.port})...", flush=True)

        proc = self._process
        pid = proc.pid
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            proc.terminate()

        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                proc.kill()
            proc.wait()
        finally:
            self._process = None

        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    def reset(self) -> None:
        """POST reset_path (if set) to clear simulator state. Best-effort."""
        if not self.reset_path:
            return
        try:
            req = urllib.request.Request(f"{self._local_url}{self.reset_path}", method="POST")
            urllib.request.urlopen(req, timeout=5.0)
        except Exception:
            pass

    def is_running(self) -> bool:
        if self.backend == "docker":
            return self._docker_is_running()
        return self._process is not None and self._process.poll() is None

    # -------------------------------------------------------------------
    # Context manager
    # -------------------------------------------------------------------

    def __enter__(self) -> "ExternalSimulator":
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
