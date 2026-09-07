"""Tests for the install hook: systemd unit generation, auto-start on auth."""

from __future__ import annotations

import shutil

import yaml
from click.testing import CliRunner

from tg_cli import daemon as d
from tg_cli.cli.main import cli


class TestUnitGeneration:
    def test_unit_template_has_required_sections(self):
        binary = "/usr/local/bin/tg"
        body = d._UNIT_TEMPLATE.format(binary=d.shlex_quote(binary), interval=5.0)
        for section in ("[Unit]", "[Service]", "[Install]"):
            assert section in body
        assert "Type=simple" in body
        assert "Restart=on-failure" in body
        assert "ExecStart=" in body
        assert "daemon run --interval 5.0" in body

    def test_unit_template_quotes_spaces_in_binary(self):
        body = d._UNIT_TEMPLATE.format(
            binary=d.shlex_quote("/opt/My App/tg"), interval=1.0
        )
        # systemd ExecStart must keep the path as one token; shlex.quote wraps.
        assert "/opt/My App/tg" in body or "'/opt/My App/tg'" in body


class TestSystemdDetection:
    def test_unavailable_when_systemctl_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(d.shutil, "which", lambda *a, **k: None)
        assert d._systemd_available() is False

    def test_unavailable_on_non_linux(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(d.sys, "platform", "darwin")
        monkeypatch.setattr(
            d.shutil, "which", lambda *a, **k: "/usr/bin/systemctl"
        )
        assert d._systemd_available() is False


class TestInstallUninstallFlow:
    def test_install_returns_unavailable_on_macos(self, monkeypatch, tmp_path):
        monkeypatch.setattr(d, "_systemd_available", lambda: False)
        result = d.install_systemd(interval=5.0)
        assert result["installed"] is False
        assert "systemd" in result["reason"]

    def test_install_writes_unit_when_available(self, monkeypatch, tmp_path):
        # Fake unit dir under tmp_path.
        unit_dir = tmp_path / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        monkeypatch.setattr(d, "_user_unit_dir", lambda: unit_dir)
        monkeypatch.setattr(d, "_systemd_available", lambda: True)
        # Prevent real systemctl calls.
        class FakeRun:
            def __init__(self):
                self.returncode = 0

            def __call__(self, *a, **k):
                return FakeRun()

        monkeypatch.setattr(d.subprocess, "run", FakeRun())
        monkeypatch.setattr(d, "shutil", shutil)
        monkeypatch.setattr(d.shutil, "which", lambda *a, **k: "/usr/bin/tg")

        result = d.install_systemd(interval=5.0)
        assert result["installed"] is True
        unit_file = unit_dir / d._UNIT_NAME
        assert unit_file.exists()
        body = unit_file.read_text()
        assert "[Service]" in body
        assert "Restart=on-failure" in body

    def test_install_idempotent(self, monkeypatch, tmp_path):
        unit_dir = tmp_path / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        monkeypatch.setattr(d, "_user_unit_dir", lambda: unit_dir)
        monkeypatch.setattr(d, "_systemd_available", lambda: True)

        class FakeRun:
            returncode = 0

        monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: FakeRun())
        monkeypatch.setattr(d.shutil, "which", lambda *a, **k: "/usr/bin/tg")

        d.install_systemd(interval=5.0)
        # Second call must succeed and not corrupt the unit file.
        d.install_systemd(interval=5.0)
        body = (unit_dir / d._UNIT_NAME).read_text()
        assert body.count("[Service]") == 1

    def test_uninstall_removes_unit(self, monkeypatch, tmp_path):
        unit_dir = tmp_path / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        unit_file = unit_dir / d._UNIT_NAME
        unit_file.write_text("[Unit]\nName=test\n")
        monkeypatch.setattr(d, "_user_unit_dir", lambda: unit_dir)
        monkeypatch.setattr(d, "_systemd_available", lambda: True)

        class FakeRun:
            returncode = 0

        monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: FakeRun())

        result = d.uninstall_systemd()
        assert result["uninstalled"] is True
        assert not unit_file.exists()

    def test_uninstall_noop_when_no_unit(self, monkeypatch, tmp_path):
        unit_dir = tmp_path / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        monkeypatch.setattr(d, "_user_unit_dir", lambda: unit_dir)
        monkeypatch.setattr(d, "_systemd_available", lambda: True)

        class FakeRun:
            returncode = 0

        monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: FakeRun())

        result = d.uninstall_systemd()
        assert result["uninstalled"] is False


class TestCliDaemonInstall:
    def test_daemon_install_help(self):
        result = CliRunner().invoke(cli, ["daemon", "install", "--help"])
        assert result.exit_code == 0
        assert "--interval" in result.output

    def test_daemon_uninstall_help(self):
        result = CliRunner().invoke(cli, ["daemon", "uninstall", "--help"])
        assert result.exit_code == 0

    def test_daemon_install_unavailable_soft_error(self, monkeypatch):
        import tg_cli.cli.tg as tg_mod

        monkeypatch.setattr(tg_mod.daemon_mod, "_systemd_available", lambda: False)
        result = CliRunner().invoke(cli, ["daemon", "install", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)["data"]
        assert data["installed"] is False


class TestAuthAutoStart:
    def test_auth_help_documents_auto_start_flag(self):
        result = CliRunner().invoke(cli, ["auth", "--help"])
        assert result.exit_code == 0
        assert "--auto-start" in result.output
        assert "--no-auto-start" in result.output

    def test_auth_failed_returns_soft_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        async def fake_fail():
            return False

        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod, "authenticate", lambda: fake_fail())
        result = CliRunner().invoke(cli, ["auth", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)
        assert data["ok"] is False
        assert data["error"]["code"] == "auth_failed"

    def test_auth_success_reports_daemon_status(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        async def fake_ok():
            return True

        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod, "authenticate", lambda: fake_ok())
        # Force all daemon bootstrap paths to fail so we hit a deterministic branch.
        monkeypatch.setattr(
            tg_mod.daemon_mod, "install_systemd",
            lambda **kw: {"installed": False, "reason": "no systemd"},
        )
        monkeypatch.setattr(tg_mod.daemon_mod, "is_daemon_alive", lambda *a, **k: False)
        monkeypatch.setattr(
            tg_mod.daemon_mod, "start_detached",
            lambda **kw: {"started": True, "pid": 99999, "log": "/tmp/x"},
        )
        result = CliRunner().invoke(cli, ["auth", "--yaml"])
        assert result.exit_code == 0
        data = yaml.safe_load(result.output)
        assert data["ok"] is True
        assert data["data"]["authenticated"] is True
        assert data["data"]["daemon"] == "started"

    def test_auth_success_no_auto_start(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        async def fake_ok():
            return True

        import tg_cli.cli.tg as tg_mod
        monkeypatch.setattr(tg_mod, "authenticate", lambda: fake_ok())
        # Track whether install was called.
        called = {"install": False}

        def fake_install(**kw):
            called["install"] = True
            return {"installed": False}

        monkeypatch.setattr(tg_mod.daemon_mod, "install_systemd", fake_install)
        monkeypatch.setattr(
            tg_mod.daemon_mod, "start_detached",
            lambda **kw: {"started": True, "pid": 1, "log": "/tmp/x"},
        )
        result = CliRunner().invoke(
            cli, ["auth", "--no-auto-start", "--yaml"]
        )
        assert result.exit_code == 0
        assert called["install"] is False
        data = yaml.safe_load(result.output)
        assert data["data"]["daemon"] is None