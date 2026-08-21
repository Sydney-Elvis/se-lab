# se-lab

Generic integration test harness for self-hosted software projects in the SydneyElvis ecosystem.

`se-lab` provides the shared infrastructure — CLI framework, Docker lifecycle management, AI-assisted failure analysis, client app orchestration, and result tracking — that product-specific lab repos build on top of. It contains no product logic and no test suites.

## Architecture

```
se-lab  (this repo, public)
    ↑
product-lab  (e.g. m3undle-lab, family-library-lab — public)
    ↑
lab.env  (per-server, gitignored — the only private layer)
```

**se-lab** provides:
- `./lab` CLI entry point and command framework
- Docker Compose lifecycle (deploy, pull, health-wait, teardown)
- Client app orchestration (`./lab clients status/update/rollback/pin` today; `up`/`down`/`reset`
  are designed — see `docs/design.md` — but not yet implemented)
- AI-assisted log analysis and failure classification via LiteLLM
- Run result tracking and report generation
- Plugin interfaces for product repos to register commands, analysis hooks, and client verification steps

**Product labs** provide:
- The `lab` shim and `scripts/agent.py` entry point (copied from `docs/templates/`, filled in
  with the product's own name — see Quick start; these can't live inside se-lab itself, since
  they locate the product lab's root from their own file position)
- Registered CLI commands (build, run, checklist, etc.)
- Test suites
- Docker Compose templates
- Fixtures and scenario seeds
- Client `verify()` implementations
- Their own `AGENTS.md`

**`lab.env`** provides:
- Server-specific values (URLs, image repos, model routes)
- Never committed, lives only on the server

## Projects using se-lab

| Repo | Status |
|------|--------|
| [m3undle-lab](https://github.com/Sydney-Elvis/m3undle-lab) | migration in progress |
| family-library-lab | planned |

## Requirements

- Python 3.12+ — enforced via `pyproject.toml`'s `requires-python`, not just documented
- Docker 24+
- A `lab.env` file (see `lab.env.example`)

Third-party Python dependencies (`PyYAML`, `requests`) are pinned in `requirements.txt` and
installed into a `.venv`, not assumed to already be present on the host. This is deliberate:
se-lab runs on hosts you don't control, so nothing it needs may depend on what a particular
base image happens to carry.

## Quick start

se-lab is a submodule inside your product lab's own repo, not something you run standalone —
`./lab` has to live at your product lab's root:

```bash
# In your product lab's own repo:
git submodule add git@github.com:Sydney-Elvis/se-lab.git se-lab
cp se-lab/docs/templates/lab ./lab
mkdir -p scripts && cp se-lab/docs/templates/scripts/agent.py scripts/agent.py
chmod +x lab
# Edit scripts/agent.py: replace the two CHANGE_ME placeholders with your
# product's name and its env var prefix (e.g. "m3undle" / "M3UNDLE").

python3.12 -m venv .venv && .venv/bin/pip install -r se-lab/requirements.txt
cp se-lab/lab.env.example lab.env
# edit lab.env with your server-specific values, and add your product's own
# <ENV_PREFIX>_* settings (repo URL, GHCR image, etc.)

./lab --help
```

se-lab itself registers no `status` command — `status`/`run`/`build`/etc. are each product lab's
own responsibility to register (see `docs/design.md`'s plugin interface example), built on top
of the Docker Compose helpers in `agent/common.py`. `./lab --help` lists whatever your product
lab has registered so far; it's the right sanity check right after scaffolding, before you've
written any commands of your own.

On a fresh VM, `scripts/setup_vm.sh` (in se-lab) can do the venv/dependency steps above for
you, plus install git/python3.12/docker/compose, create required directories, and run a
preflight check:

```bash
bash se-lab/scripts/setup_vm.sh --product-name yourproduct --env-prefix YOURPRODUCT
```

`--product-name`/`--env-prefix` must match what your `scripts/agent.py` passes to
`agent.runtime.configure()`. Run `bash se-lab/scripts/setup_vm.sh --help` for the full flag
list (`--base-dir`, `--extra-packages`, etc.).

## Status

Under active development. API and plugin interfaces are not yet stable.
