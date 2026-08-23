# se-lab Design

## Goals

- Provide a reusable, product-agnostic integration test harness
- Let product labs be fully public (tests, fixtures, compose templates) with `lab.env` as the only private layer
- Support both automated test pipelines (M3Undle, FL unit/integration) and manual client exploration
- Make the `./lab clients` abstraction first-class so it serves both M3Undle (manual discovery) and FL (automated delivery verification)
- Be publishable and readable by anyone evaluating the products that use it

## Three-Layer Model

```
┌─────────────────────────────────────────────────┐
│  lab.env  (per-server, gitignored)              │
│  Server URLs, image repos, AI model routes      │
└───────────────────┬─────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────┐
│  product-lab  (public repo)                     │
│  Test suites, fixtures, compose templates,      │
│  CLI command plugins, client verify() impls     │
└───────────────────┬─────────────────────────────┘
                    │ git submodule (or pip install)
┌───────────────────▼─────────────────────────────┐
│  se-lab  (this repo, public)                    │
│  CLI framework, Docker helpers, AI routing,     │
│  client lifecycle, result tracking              │
└─────────────────────────────────────────────────┘
```

## Components

### CLI Framework (`agent/`)

The `./lab` entry point delegates to a Python agent package. Product repos register their commands and product-specific plugins at startup; `se-lab` provides the runtime (argument parsing, dashboard rendering, progress bars, confirmation prompts, AI routing).

**Tier model for AI tasks:**

| Tier | Use |
|------|-----|
| fast | Quick classifications, single-field extractions |
| light | Short summaries, preflight checks |
| standard | Log analysis, failure context |
| reasoner | Root cause analysis across multiple failures |
| large | Full run reports, cross-suite correlation |

All AI traffic routes through a single LiteLLM endpoint (`LAB_LITELLM_URL`). Model assignments per tier are configured in `lab.env` — se-lab ships no opinionated defaults that reference specific hardware.

AI analysis runs only on failure or explicit request (`./lab analyze`, `./lab doctor ai`). It never runs on passing suites.

**Plugin interface — command registration:**

```python
# se-lab is a submodule at <product-lab>/se-lab/; scripts/agent.py adds that
# directory to sys.path before importing anything from agent/, so the
# importable package is `agent`, not `se_lab` -- there is no se_lab package.
# In product-lab's own commands package:
from agent import registry

@registry.command("run", help="Deploy and run the full test suite")
def cmd_run(args, config):
    ...

@registry.command("build", help="Build from branch")
def cmd_build(args, config):
    ...
```

Note: `down` is the one lifecycle verb se-lab already registers as a built-in itself
(`agent/commands/down.py`, calls `agent.common.compose_down()`) since "stop the one compose
stack this product lab deploys" generalizes across products. `registry.command()` raises on a
duplicate name, so a product lab cannot also register a top-level `down` — pick a different name
for anything narrower than that (e.g. tearing down one named profile while leaving another
running).

**Plugin interface — analysis hooks:**

```python
# Product lab provides product-specific log parsing and classification rubrics
from agent.analysis.plugin import AnalysisPlugin

class MyProductAnalysis(AnalysisPlugin):
    def extract_log_context(self, logs: str) -> str: ...
    def classification_rubric(self) -> str: ...
    def failure_prompt(self, context: str) -> str: ...
```

### Docker Helpers (`agent/common.py`)

Generic Docker Compose lifecycle utilities — call these from your own registered commands, don't
reimplement `docker compose` calls by hand:
- `compose_command()`, `compose_up()`, `compose_up_only()`, `compose_down()`, `compose_ps()`,
  `compose_logs()` — Compose up/down/status/log capture, with `extra_compose_files=` for
  scenario-specific overrides layered on your base compose file
- Environment loading from `lab.env`
- Deployment metadata (image digest, timestamp, branch)

No product-specific ports, service names, or compose fragments.

### Client App Orchestration (`./lab clients`)

**Implemented today:** `clients status`, `clients update`, `clients rollback`, `clients pin`
(`agent/commands/clients.py`).

**Designed, not yet implemented** — first-class support for bringing up external client services
alongside the product under test:
```
./lab clients up [--profile <name>]   # start selected clients
./lab clients down                     # stop and remove
./lab clients reset [--profile <name>] # wipe state, return to clean baseline
```

**Profile-based selection:** A product lab defines named profiles (e.g. `cwa-sftp`, `abs`, `jellyfin`) mapping to sets of compose services. `./lab clients up --profile cwa-sftp` brings up only the relevant services.

**Client verification interface:**

```python
from agent.clients.plugin import ClientPlugin

class CWAClient(ClientPlugin):
    name = "cwa"
    compose_service = "calibre-web-automated"

    def ready(self) -> bool:
        # Return True when client is healthy and ready to receive
        ...

    def verify(self, context: TestContext) -> VerifyResult:
        # Assert the expected outcome occurred (book appeared, etc.)
        # Return VerifyResult(passed=True/False, detail="...")
        ...
```

For clients that are manual-only (e.g. Jellyfin for M3Undle), `verify()` is not implemented and `se-lab` falls through to checklist generation.

### Settings Archives (`./lab settings`)

Product applications own their settings archive format and API calls. se-lab provides the common
command surface through a registered `SettingsPlugin`, which makes the same settings-only archive
workflow usable for both standalone lab setup and automated integration verification:

```bash
# Seed a fresh disposable instance before standalone exploration.
lab run --fresh
lab settings import fixtures/settings/lab-baseline.<product-extension>

# In an automated integration suite: export, reset, import, then verify connections.
lab settings export --out "$LAB_ARTIFACTS_DIR/settings-under-test.<product-extension>"
lab recreate --fresh
lab settings import "$LAB_ARTIFACTS_DIR/settings-under-test.<product-extension>"
```

`lab settings export [--out PATH]` always produces a settings-scoped archive. Without `--out`,
the archive is written under `LAB_ARTIFACTS_DIR` (or the standard ad-hoc artifacts directory)
using the plugin's default filename. `lab settings import <path>` applies an archive and prints
the plugin's structured summary of changed entities.

```python
from pathlib import Path

from agent import registry
from agent.settings.plugin import SettingsPlugin


class MyProductSettings(SettingsPlugin):
    def export_settings(self, out_path: Path) -> None:
        # Call the product's settings-only export API and write its archive here.
        ...

    def import_settings(self, archive_path: Path) -> dict:
        # Call the product's settings-only import API and return applied entities/counts.
        return {"entities": {"provider_settings": 2}}

    def default_export_filename(self) -> str:
        return "settings-backup.myproduct"

    def capability(self) -> str:
        # Probe the deployed API/version or feature flag when support is conditional.
        return "settings-only"


registry.set_settings_plugin(MyProductSettings())
```

Return `"unsupported"` from `capability()` when a deployed image has not exposed the feature.
se-lab will then stop before it calls either operation.

Settings archives are expected to be encrypted/authenticated under a passphrase (they carry live
provider/integration secrets as a portable, potentially fixture-checked-in file — see
`.ai_docs/settings-backup-restore-plan.md`). `export_settings`/`import_settings` deliberately take
no passphrase parameter, so this stays the product's own concern rather than a change to the ABC:
call `agent.common.settings_passphrase()` from inside your implementation, which reads
`<ENV_PREFIX>_SETTINGS_PASSPHRASE` via the same env/lab.env precedence as every other
ENV_PREFIX-parametrized setting. Document the pinned lab value in your product lab's own
`lab.env.example`, alongside its other `<ENV_PREFIX>_*` variables.

### Webhook Receiver (`agent/webhook_receiver.py`)

A generic test double for a product's outbound webhook/callback integrations. Not a plugin --
there's no per-product behavior here, just a small local HTTP server that records every request
it receives, with no knowledge of any particular payload shape:

```python
from agent.webhook_receiver import WebhookReceiver

with WebhookReceiver() as receiver:
    configure_product_webhook(f"http://{gateway_ip}:{receiver.port}/hook")
    trigger_the_thing_that_should_fire_it()
    request = receiver.wait_for_request(timeout=30.0)
    assert request is not None
    assert request.json()["event"] == "expected-event"
```

Binds `0.0.0.0` by default so a containerized product can reach it via `agent.container.get_docker_gateway()`'s
IP, while the test process itself talks to it over `127.0.0.1`. `receiver.requests` and
`receiver.clear()` inspect/reset what's been recorded so far; `wait_for_request(predicate=...)`
polls until a matching request arrives or times out.

### Result Tracking (`scripts/common/harness/results.py`)

Each test run produces a `RunContext` that records:
- Per-suite pass/fail counts and timing
- Expected vs actual test totals (drift detection)
- JSON artifact per suite (`results-<suite>.json`)
- Rolled-up summary for AI analysis input

Schema is product-agnostic. Product labs write results using the `RunContext` API; se-lab reads them for reporting and analysis.

### Reporting (`agent/reporting/`)

- Per-run markdown report
- AI metrics log (task type, model, latency, tokens)
- Drift warnings when declared suite totals don't match actual

### Bootstrap & Dependencies

The design goal is: run the setup script, get a fully working test environment, with no manual
"install whatever's missing" step afterward. That only holds if the interpreter version and
every third-party dependency are declared and installed by the tooling — not inherited from
whatever a given base image happens to carry.

- **Python 3.12+, enforced** via `pyproject.toml`'s `requires-python`, not just documented.
  se-lab's minimum floor is set by real language features in use (`datetime.UTC` needs 3.11,
  `dataclass(slots=True)` needs 3.10); 3.12 is the version actually validated, and pinning
  above the true floor is easier to relax later than to tighten.
- **Third-party dependencies are pinned in `requirements.txt`** (`PyYAML`, `requests`) and
  installed into a per-lab `.venv` — never assumed present, never installed system-wide. A
  config file that fails to parse because a dependency is missing must raise, naming the file
  and the fix — not silently fall back to defaults. (`agent/config.py` does this for YAML.)
- **`setup_vm.sh`** (bootstrap, in progress) installs `python3.12` explicitly rather than a
  bare `python3`, creates `.venv`, installs `requirements.txt` into it, and the `./lab` shim
  execs that interpreter — so every entry path gets the pinned versions, not the system default.
- **A preflight self-check** runs at the end of setup and prints a PASS/FAIL summary: Python
  version, each dependency importable, Docker/Compose v2/Buildx present, and the docker group
  membership actually usable without a re-login. The setup script should be able to say whether
  it succeeded, not leave that to be discovered at first use.

## Configuration (`lab.env`)

All server-specific values live here. Never committed.

```bash
# AI endpoint
LAB_LITELLM_URL=http://<your-litellm-host>:4000/v1
LAB_LITELLM_READ_TIMEOUT_SEC=90

# Model tier assignments (all optional — product labs provide fallback labels)
LAB_MODEL_FAST=<model>
LAB_MODEL_LIGHT=<model>
LAB_MODEL_STANDARD=<model>
LAB_MODEL_REASONER=<model>
LAB_MODEL_LARGE=<model>

# Product-lab specific values are namespaced by the product lab
# e.g. M3UNDLE_GHCR_IMAGE, M3UNDLE_RUNTIME_DIR, FL_POSTGRES_PASSWORD
```

## Parallel Operation Strategy

During migration from m3undle-lab to the new se-lab + m3undle-lab structure, both harnesses run on toontown-int-srv1 simultaneously:

- Old: `/opt/m3undle-lab` — existing harness, untouched
- New: `/opt/m3undle-lab-v2` (or similar) — new structure, separate runtime dir

Both target the same M3Undle instance. Test results are compared suite-by-suite to confirm parity before the old harness is decommissioned. See `.ai_docs/roadmap.md` for the step-by-step migration plan.

## What se-lab Does NOT Contain

- Any product test suites
- Any product-specific HTTP clients
- Docker Compose templates (those live in product labs)
- Fixtures or test data
- Hardcoded service names, ports, or URLs
- AI model names (all via `lab.env`)
