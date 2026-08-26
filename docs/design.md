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
- `published_ports()` / `print_published_ports()` — host ports currently published by this
  project's containers, read from `compose ps --format json`. Every `deploy_*()` helper below
  prints this right after bringing the stack up, so `./lab up` always ends with a list of what's
  actually reachable instead of just "deployed."
- Environment loading from `lab.env`
- Deployment metadata (image digest, timestamp, branch)

No product-specific ports, service names, or compose fragments.

### Client App Orchestration (`./lab clients`)

**Implemented today:** `clients status`, `clients update`, `clients rollback`, `clients pin`,
`clients up`, `clients down`, `clients reset` (`agent/commands/clients.py`).

```
./lab clients up [--profile CLIENT ...]   # bring up the product stack plus selected clients
./lab clients down                        # stop/remove clients, leave the product stack running
./lab clients reset [--profile CLIENT ...] # wipe client state, recreate clean
```

**Selection today rides Docker Compose's own `profiles:` field directly** — each `ClientPlugin`
name doubles as its compose service's profile name, and `--profile` sets `COMPOSE_PROFILES` (the
same env var `active_clients()` already reads). `up`/`reset` default to "every registered
client"/"whatever's currently active" when `--profile` is omitted. A product lab registers the
compose file(s) these commands layer on top of the base stack once, via
`registry.set_client_compose_files([...])` — typically a network-topology override plus the
client services themselves — the same optional-hook shape as `registry.set_layout_hook()`.

**Not built**: named multi-service profiles (e.g. a single `cwa-sftp` profile bundling several
compose services together, as opposed to one profile per client). The flat one-client-one-profile
model above is deliberately the simpler thing that was actually needed first; family-librarian-lab
may need the richer grouping once it registers its own `ClientPlugin`s (Phase 2b) — revisit then,
not speculatively now.

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

### Status Reporting (`agent/status.BaseStatus`)

`status` is not a se-lab built-in and never will be — naming stays with the product lab, same as
`run`/`build`/`recreate`/etc. (see `agent/cli.py`'s docstring). What's shared instead is a base
class every product lab's own `status` command subclasses, so the runtime/deployment/compose-ps/
client reporting — identical on every lab — is written once instead of hand-rolled per product.
This is the standard shape for any future cross-lab command: the product lab keeps ownership of
registration, se-lab supplies a subclassable base that does the generic 90%, following the same
pattern as `ClientPlugin`/`AnalysisPlugin`/`SettingsPlugin` above rather than a registration
callback invented just for this one command.

```python
from agent.status import BaseStatus

class FamilyLibrarianStatus(BaseStatus):
    def extra(self) -> int:
        # Product-specific probes (HTTP health, readiness, DB state, ...).
        # Print whatever you want appended, then return the exit code `status` should use.
        result = _readiness_probe()
        print(json.dumps({"health": result}, indent=2), flush=True)
        return 0 if result["ok"] else 1

@registry.command("status", help="Show Family Librarian status")
def handle_status(args: argparse.Namespace, config: Config) -> int:
    return FamilyLibrarianStatus().run()
```

`BaseStatus.run()` prints, in order:
- `deployment_lines()` — runtime dir, current image, deployment source/commit/update time,
  deployed source checkout branch/commit, last test result. All read from `common.py`'s existing
  deployment-metadata helpers.
- `print_compose_state()` — `compose ps` plus `published_ports()` for every service. This is
  where "what application is up, what port is it listening on" lives: the application is just
  another compose service, no different from a client's, so it needs no special-casing.
- `client_lines()` — one line per client active in `COMPOSE_PROFILES`, cross-referencing each
  registered `ClientPlugin`'s `compose_service` against `published_ports()` for
  version/ready/ports. *Which* clients exist (Jellyfin, CWA, ABS, ...) is per-product; "list the
  active ones with version/ready/ports" isn't.
- `extra()` — the product-specific hook above, default no-op returning exit 0.

Every method is independently overridable, not just `extra()` — a product lab that wants to
reorder, drop, or annotate a generic section overrides that one method and calls `super()` itself
rather than treating the base report as opaque.

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

## Guardrail: Where New Lifecycle Code Belongs

**This section exists because the drift it describes already happened once and cost real
audit/reconstruction time to find and undo.** family-librarian-lab was scaffolded without going
through this check, ended up with its own hand-rolled `docker compose ps --format json` parser
next to se-lab's, and that parser only handled one of the two shapes Compose actually emits (see
`_compose_ps_entries()`'s history below) -- a real bug, not just duplication, sitting undetected
in a second copy of logic se-lab had already gotten wrong once and fixed. Don't let it happen a
third time.

**Before writing any new subprocess/compose/env-loading/suite-selection/status-reporting code in
a product lab, run this check first, in order:**

1. **Does `agent.common`, `agent.suites`, `agent.status`, or an existing plugin ABC
   (`ClientPlugin`/`AnalysisPlugin`/`SettingsPlugin`/`DatabasePlugin`) already do this?** Grep
   se-lab before writing a subprocess wrapper, an env-file loader, a `compose ps` parser, a suite
   selector, or a status report section. If it exists, call it. Don't re-derive it because the
   product lab's own file already imports fewer things, or because it "seemed small enough to
   just write."
2. **If it doesn't exist yet, is it product-specific, or would any second product lab need the
   same thing?** Use the three-tier split already applied throughout `agent/common.py` (see that
   file's own module docstring) as the actual test, not a vibe check:
   - **Tier A -- fully generic:** ports to se-lab unchanged. (Subprocess lifecycle, JSON/text
     parsing of a third-party tool's output, port-conflict detection, dashboard coordination --
     none of this cares what product is under test.)
   - **Tier B -- generic mechanism, product-specific naming only:** ports to se-lab, parametrized
     by `runtime.PRODUCT_NAME`/`runtime.ENV_PREFIX` (see `agent/runtime.py`'s `configure()`) the
     same way `_repo_url_env_key()`, `_ghcr_image_env_key()`, `settings_passphrase()`, etc.
     already are. A product-specific *name* is not the same thing as product-specific *logic*.
   - **Tier C -- real product logic:** stays in the product lab. Credential bootstrapping, a
     specific client app's own state manipulation, hardcoded network/image defaults for a
     specific app.
   If step 2 lands on Tier A or B and a second consumer would plausibly want it, that is the
   signal to add it to se-lab now, not to write it locally "for now" and reconcile later --
   "later" is how family-librarian-lab ended up with a second, worse copy of `_compose_ps_entries()`'s
   already-solved shape problem.
3. **Does the mechanism assume se-lab's single fixed `project_name()`/one-stack-per-lab model, or
   does the product lab need its own ad-hoc/per-scenario project naming?** That's a real
   architectural difference (family-librarian-lab's disposable-project-per-test-case model is not
   a mistake), but it is *not* a license to reimplement everything downstream of that one
   difference. Split the reusable, naming-independent part out as a pure function the product lab
   can call directly (see `parse_compose_ps_json()`, extracted from `_compose_ps_entries()`
   specifically so a lab running its own ad-hoc `docker compose -p <name> ps --format json`
   still gets the shape-handling for free) rather than treating "our project-naming is different"
   as justification for owning the whole subprocess-to-parsed-result path again.

**When you do add something to se-lab as a result of this check:** write it with tests that cover
the edge case that motivated it (the array-vs-JSON-lines split has two dedicated tests in
`tests/test_common.py` for exactly this reason), document which tier it is and why in a docstring
or comment the way the rest of `agent/common.py` already does, and update this doc's Components
section so the next person doesn't have to rediscover it exists.

**Submodule pins are part of this guardrail, not a separate concern.** A product lab pinned many
commits behind se-lab's `main` is a product lab slowly re-growing duplication it has no way to see
-- `select_suites()` was extracted specifically because two product labs had independently
hand-rolled the same suite/case-selection logic, and a stale pin is exactly what let a *third*
hand-rolled copy exist again in the interim. Bump a product lab's se-lab pin regularly (not only
when a specific new feature is needed), and when you bump it, actually check the commits crossed
for anything the product lab should now delete its own copy of.
