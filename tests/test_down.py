"""agent/commands/down.py: `lab down` sweeps every Compose project for this
product lab via lab_common.compose_down_all(), without needing real docker."""

from __future__ import annotations

import agent.cli as cli
from agent import common as lab_common


def test_main_down_keeps_volumes_by_default(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        lab_common,
        "compose_down_all",
        lambda **kw: (calls.append(kw), ["selftest-lab", "selftest-lab-scenario"])[1],
    )
    code = cli.main(["down"])
    assert code == 0
    assert calls == [{"remove_volumes": False}]
    out = capsys.readouterr().out
    assert "Stopped 2 Compose project(s): selftest-lab, selftest-lab-scenario" in out
    assert "Volumes kept -- rerun with --clean-volumes" in out


def test_main_down_clean_volumes_removes_them(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        lab_common,
        "compose_down_all",
        lambda **kw: (calls.append(kw), ["selftest-lab"])[1],
    )
    code = cli.main(["down", "--clean-volumes"])
    assert code == 0
    assert calls == [{"remove_volumes": True}]
    out = capsys.readouterr().out
    assert "Volumes kept" not in out


def test_main_down_reports_nothing_found(monkeypatch, capsys):
    monkeypatch.setattr(lab_common, "compose_down_all", lambda **kw: [])
    code = cli.main(["down"])
    assert code == 0
    out = capsys.readouterr().out
    assert "No Compose projects found for 'selftest-lab'" in out


def test_down_help_shows_a_real_description(capsys):
    parser = cli.build_parser()
    try:
        parser.parse_args(["down", "--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "every Compose project" in out
    assert "--clean-volumes" in out
