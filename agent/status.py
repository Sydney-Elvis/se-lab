"""Shared base for `lab status`: the runtime/deployment/compose/client report
every product lab needs, meant to be subclassed for whatever product-specific
detail (application health probes, DB state, ...) a lab wants to add.

se-lab still doesn't register `status` as a command itself (see agent/cli.py's
note on `status`/`run`/`build`/... having no se-lab built-in, unlike `down`)
-- a product lab keeps owning that name and registering it. What this saves
is every product lab hand-rolling the runtime/deployment/compose-ps/client
boilerplate that's identical everywhere to do it, and it follows the same
subclass-a-plugin-class shape already used for ClientPlugin/AnalysisPlugin/
SettingsPlugin rather than inventing a new registration-callback pattern for
just this one command.

    from agent.status import BaseStatus

    class FamilyLibrarianStatus(BaseStatus):
        def extra(self) -> int:
            result = _readiness_probe()
            print(json.dumps({"health": result}, indent=2), flush=True)
            return 0 if result["ok"] else 1

    @registry.command("status", help="Show Family Librarian status")
    def handle_status(args: argparse.Namespace, config: Config) -> int:
        return FamilyLibrarianStatus().run()

Every method below is individually overridable, not just extra() -- a
product lab that wants to reorder, drop, or annotate a generic section can
override that one method and call super() itself, rather than accepting the
base report as opaque.
"""

from __future__ import annotations

from . import common as lab_common
from . import registry


class BaseStatus:
    def deployment_lines(self) -> list[str]:
        """Runtime dir, current image, deployment source/commit/update time,
        deployed source checkout state, last test result -- read straight out
        of common.py's existing deployment-metadata helpers, so this part is
        identical on every lab."""
        lines = [f"Runtime: {lab_common.runtime_summary()}"]

        metadata = lab_common.get_deployment_metadata()
        lines.append(f"Current configured image: {metadata['image'] or 'not set'}")
        if metadata["source_type"] and metadata["source_ref"]:
            inferred = " (inferred)" if metadata.get("source_inferred") else ""
            lines.append(f"Deployment source: {metadata['source_type']} {metadata['source_ref']}{inferred}")
        if metadata["source_commit"]:
            lines.append(f"Deployment commit: {metadata['source_commit']}")
        if metadata["updated_at_utc"]:
            lines.append(
                f"Last deployment update: {metadata['updated_at_utc']} on {metadata['updated_host'] or 'unknown host'}"
            )

        if lab_common.is_git_checkout(lab_common.repo_dir()):
            branch = lab_common.repo_current_branch() or "detached"
            commit = lab_common.repo_head_commit(short=True) or "unknown"
            lines.append(f"Deployed source checkout: branch={branch} commit={commit}")

        last_test = lab_common.get_last_test_metadata()
        if last_test["target_type"] and last_test["target_ref"]:
            lines.append(f"Last test target: {last_test['target_type']} {last_test['target_ref']}")
        if last_test["status"]:
            when = f" at {last_test['at_utc']}" if last_test["at_utc"] else ""
            host = f" on {last_test['host']}" if last_test["host"] else ""
            lines.append(f"Last test result: {last_test['status']}{when}{host}")
        return lines

    def print_compose_state(self) -> None:
        """`compose ps` plus every published port, for every service --
        application, clients, and infra (postgres, clamav, ...) alike. This
        is where "what application is up, what port is it listening on"
        lives: the application is just another compose service, no different
        from a client's, so it needs no special-casing here. Skipped
        entirely when the product lab hasn't defined a compose stack yet."""
        if not lab_common.has_compose_stack():
            print("No compose stack defined yet.", flush=True)
            return
        lab_common.compose_ps()
        lab_common.print_published_ports()

    def client_lines(self) -> list[str]:
        """One line per client active in COMPOSE_PROFILES: version, ready
        state, and the host ports its own compose service publishes
        (cross-referenced from published_ports() by ClientPlugin.compose_service,
        not re-probed). Which clients exist (jellyfin, cwa, abs, ...) is
        per-product -- registered ClientPlugins -- but "list the active ones
        with version/ready/ports" is identical across labs."""
        active = lab_common.active_clients()
        if not active:
            return []
        ports_by_service: dict[str, list[str]] = {}
        for service, mapping in lab_common.published_ports():
            ports_by_service.setdefault(service, []).append(mapping)

        lines = ["Clients:"]
        for name in active:
            plugin = registry.get_client(name)()
            version = plugin.detect_version() or "unknown"
            ready = "ready" if plugin.ready() else "not ready"
            ports = ", ".join(ports_by_service.get(plugin.compose_service, [])) or "no published ports"
            lines.append(f"  {name}: {ready}, version={version}, ports={ports}")
        return lines

    def extra(self) -> int:
        """Override in a product lab subclass to print additional,
        product-specific sections (application health probes, DB state,
        whatever) and return the process exit code `status` should use.
        Default: nothing to add, exit 0."""
        return 0

    def run(self) -> int:
        for line in self.deployment_lines():
            print(line, flush=True)
        self.print_compose_state()
        for line in self.client_lines():
            print(line, flush=True)
        return self.extra()
