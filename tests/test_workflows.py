"""Tests for the GitHub Actions workflows (ci.yml + publish.yml).

We don't run actions in CI, but we lint the YAML structure, validate
expressions, and exercise the same commands the workflows run.
"""

# ruff: noqa: I001
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


# ─────────────────────────── YAML structural validation ───────────────────────────


class TestWorkflowStructure:
    def test_ci_and_publish_exist(self):
        assert (WORKFLOWS / "ci.yml").is_file()
        assert (WORKFLOWS / "publish.yml").is_file()

    def test_yaml_parses(self):
        for name in ("ci.yml", "publish.yml"):
            with open(WORKFLOWS / name) as f:
                yaml.safe_load(f)

    def test_publish_reuses_ci_via_workflow_call(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        verify = pub["jobs"].get("verify")
        assert verify is not None, "publish must have a 'verify' job that gates deploys"
        assert verify.get("uses") == "./.github/workflows/ci.yml", (
            "publish must call ci.yml to avoid divergent test/lint logic"
        )

    def test_publish_uses_official_pypi_publish_action(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        steps = "\n".join(s.get("uses", "") for s in pub["jobs"]["publish"]["steps"])
        # Must use the official PyPA trusted-publishing action, not raw twine.
        assert "pypa/gh-action-pypi-publish@release/v1" in steps, (
            "publish must use pypa/gh-action-pypi-publish@release/v1 (Trusted Publisher)"
        )

    def test_publish_has_trusted_publisher_id_token(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        perms = pub["jobs"]["publish"].get("permissions") or {}
        assert perms.get("id-token") == "write", (
            "OIDC id-token:write is required for PyPI Trusted Publishing"
        )
        assert perms.get("contents") == "read"

    def test_publish_tag_trigger_includes_v_prefix(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        triggers = pub[True] if True in pub else pub["on"]
        tags = triggers["push"]["tags"]
        assert "v*" in tags or any(re.match(r"v\W", t) for t in tags), (
            f"publish must trigger on v* tags, got {tags}"
        )

    def test_ci_workflow_call_trigger(self):
        with open(WORKFLOWS / "ci.yml") as f:
            ci = yaml.safe_load(f)
        triggers = ci[True] if True in ci else ci["on"]
        assert "workflow_call" in triggers, (
            "ci.yml must declare workflow_call so publish.yml can reuse it"
        )

    def test_ci_tests_and_lints_against_pyproject(self):
        with open(WORKFLOWS / "ci.yml") as f:
            ci = yaml.safe_load(f)
        steps_text = yaml.dump(ci["jobs"]["lint-and-test"]["steps"])
        assert "uv run python -m pytest" in steps_text
        assert "uv run ruff check" in steps_text

    def test_ci_build_uses_uv_build(self):
        with open(WORKFLOWS / "ci.yml") as f:
            ci = yaml.safe_load(f)
        steps_text = yaml.dump(ci["jobs"]["build"]["steps"])
        # Use the canonical PEP 517 build invocation, not the bare 'uv build'
        # shortcut (which would force-install twine into the project env).
        assert "python -m build" in steps_text
        assert "twine check" in steps_text

    def test_ci_python_versions_are_supported(self):
        with open(WORKFLOWS / "ci.yml") as f:
            ci = yaml.safe_load(f)
        versions = ci["jobs"]["lint-and-test"]["strategy"]["matrix"]["python-version"]
        # pyproject requires-python=">=3.10" — matrix must cover all supported.
        for v in versions:
            major, minor = (int(x) for x in v.split("."))
            assert (major, minor) >= (3, 10)
        # Must include 3.12 (publish builds on it).
        assert "3.12" in versions, "publish builds on 3.12, CI must too"

    def test_publish_pins_python_to_one_version(self):
        # Don't waste CI minutes on a matrix in the publish job.
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        steps = pub["jobs"]["publish"]["steps"]
        py_setup = next(s for s in steps if s.get("uses", "").startswith("actions/setup-python"))
        assert "matrix" not in py_setup["with"]["python-version"], (
            "publish job must use a fixed Python version, not a matrix"
        )

    def test_no_duplicate_job_names(self):
        seen: dict[str, str] = {}
        for wf in ("ci.yml", "publish.yml"):
            with open(WORKFLOWS / wf) as f:
                jobs = yaml.safe_load(f)["jobs"]
            for name, _body in jobs.items():
                if name in seen and seen[name] != wf:
                    pytest.fail(f"duplicate job name '{name}' across {wf} and {seen[name]}")
                seen[name] = wf

    def test_no_pinned_action_shas_missing(self):
        # Documented best practice: pin third-party actions to a SHA. We don't
        # require SHAs but ensure all `uses:` references are on tagged versions,
        # not floating branches, to avoid supply-chain surprises.
        for wf in ("ci.yml", "publish.yml"):
            with open(WORKFLOWS / wf) as f:
                content = f.read()
            for m in re.finditer(r"uses:\s*([^\s#]+)", content):
                ref = m.group(1)
                if ref.startswith("./"):
                    continue
                # Allow @release/v1 style, @vN, @<sha>; reject @main / @master.
                if "@" in ref:
                    target = ref.split("@", 1)[1]
                    assert target not in ("main", "master"), (
                        f"{wf}: action {ref} must not track a floating branch"
                    )

    def test_workflow_has_least_privilege_permissions(self):
        # Both workflows must declare top-level permissions: contents: read
        # and rely on jobs to opt in to more (e.g., id-token for publish).
        for wf in ("ci.yml", "publish.yml"):
            with open(WORKFLOWS / wf) as f:
                data = yaml.safe_load(f)
            assert "permissions" in data, f"{wf} must declare top-level permissions"
            assert data["permissions"].get("contents") == "read", (
                f"{wf} must default to contents: read (got {data['permissions']})"
            )

    def test_workflow_has_timeout(self):
        # A hung job should not pin a runner for 6 hours. The verify job in
        # publish.yml is a re-use of ci.yml, so it's exempt from this check.
        for wf in ("ci.yml", "publish.yml"):
            with open(WORKFLOWS / wf) as f:
                data = yaml.safe_load(f)
            for name, body in data["jobs"].items():
                if "uses" in body:
                    # Reusable workflow call — its timeouts are owned by ci.yml.
                    continue
                assert "timeout-minutes" in body, (
                    f"{wf}.{name} must declare timeout-minutes"
                )
                assert body["timeout-minutes"] <= 30, (
                    f"{wf}.{name} timeout-minutes too long: {body['timeout-minutes']}"
                )

    def test_workflow_has_concurrency_group(self):
        for wf in ("ci.yml", "publish.yml"):
            with open(WORKFLOWS / wf) as f:
                data = yaml.safe_load(f)
            # concurrency is at the top level, not under `on`.
            assert "concurrency" in data, (
                f"{wf} must declare a concurrency group to cancel superseded runs"
            )
            assert "group" in data["concurrency"]
            assert "cancel-in-progress" in data["concurrency"]

    def test_ci_has_pr_trigger(self):
        # CI must run on PRs so contributors catch breakage before merge.
        with open(WORKFLOWS / "ci.yml") as f:
            data = yaml.safe_load(f)
        triggers = data[True] if True in data else data["on"]
        assert "pull_request" in triggers, (
            "ci.yml must trigger on pull_request to gate PRs"
        )

    def test_ci_uploads_dist_artifact(self):
        with open(WORKFLOWS / "ci.yml") as f:
            data = yaml.safe_load(f)
        steps = data["jobs"]["build"]["steps"]
        text = yaml.dump(steps)
        assert "actions/upload-artifact" in text, (
            "ci.yml must upload dist/ as an artifact for inspection"
        )

    def test_publish_verifies_tag_matches_version(self):
        # Pushing v0.9.0 when pyproject says 0.8.1 must fail.
        with open(WORKFLOWS / "publish.yml") as f:
            data = yaml.safe_load(f)
        steps_text = yaml.dump(data["jobs"]["publish"]["steps"])
        assert "GITHUB_REF_NAME" in steps_text, (
            "publish must verify tag version against pyproject version"
        )
        assert "pyproject" in steps_text, (
            "publish must read pyproject.toml to extract the package version"
        )

    def test_publish_runs_twine_check(self):
        # Defense in depth: re-check the built dist before publish.
        with open(WORKFLOWS / "publish.yml") as f:
            data = yaml.safe_load(f)
        steps = data["jobs"]["publish"]["steps"]
        text = yaml.dump(steps)
        assert "twine check" in text, (
            "publish must run 'twine check' before pushing to PyPI"
        )

    def test_publish_no_silent_skip(self):
        # `if: success()` is redundant (default) and `if: always()` is dangerous.
        with open(WORKFLOWS / "publish.yml") as f:
            data = yaml.safe_load(f)
        pub = data["jobs"]["publish"]
        assert "if" not in pub, (
            "publish job must not have an 'if' override (default: needs satisfied)"
        )


# ─────────────────────────── pyproject consistency ───────────────────────────


class TestPyprojectConsistency:
    def test_hatchling_pin_present(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        reqs = data["build-system"]["requires"]
        assert any("hatchling" in r for r in reqs), "must pin hatchling"

    def test_version_matches_tag_or_unspecified(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        ver = data["project"]["version"]
        # Tag vX.Y.Z maps to version "X.Y.Z" by convention.
        assert re.match(r"^\d+\.\d+\.\d+$", ver), f"version '{ver}' is not semver"

    def test_dev_group_lists_uv_install_dependencies(self):
        # uv sync --group dev must install everything CI needs.
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        dev = data.get("dependency-groups", {}).get("dev", [])
        joined = " ".join(dev).lower()
        assert "pytest" in joined
        assert "ruff" in joined
        assert "twine" in joined

    def test_no_dependencies_on_pypi_internal_packages(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        deps = data["project"]["dependencies"]
        for d in deps:
            # pip-style direct URL references break PEP 517 sdist build.
            assert " @ " not in d, f"dependency {d!r} uses PEP 508 URL — bad for sdist"

    def test_license_file_exists(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        lic = data["project"].get("license", {}).get("file")
        if lic:
            assert (ROOT / lic).is_file(), f"license file {lic} missing"

    def test_readme_exists(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        rm = data["project"].get("readme")
        if rm:
            assert (ROOT / rm).is_file()

    def test_sdist_include_files_exist(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        include = data["tool"]["hatch"]["build"]["targets"]["sdist"].get("include", [])
        for entry in include:
            p = ROOT / entry
            # Recursive globs are allowed.
            if "*" in entry:
                continue
            assert p.exists(), f"sdist include {entry} missing"


# ─────────────────────────── Live build validation ───────────────────────────


class TestBuildReproducibility:
    """Re-run the build steps from ci.yml in a temp dir to ensure they work.

    These tests build the wheel/sdist in a fresh venv, which is the canonical
    CI flow. They are slow (~3 min total) — opt in with ``-m slow`` or run
    before tagging a release.
    """

    @pytest.mark.slow
    def test_uv_build_succeeds(self, tmp_path):
        # Use a fresh venv so we don't pick up local .venv.
        subprocess.check_call(
            [sys.executable, "-m", "venv", str(tmp_path / "venv")],
            cwd=ROOT,
        )
        venv_py = tmp_path / "venv" / "bin" / "python"
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install", "--quiet", "build", "twine"],
            cwd=ROOT,
        )
        # Install the project itself in editable mode so the wheel has all modules.
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install", "--quiet", "-e", "."],
            cwd=ROOT,
        )
        # Build
        result = subprocess.run(
            [str(venv_py), "-m", "build", "--outdir", str(tmp_path / "dist")],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, f"build failed:\n{result.stdout}\n{result.stderr}"
        whl = list((tmp_path / "dist").glob("*.whl"))
        sdist = list((tmp_path / "dist").glob("*.tar.gz"))
        assert len(whl) == 1 and len(sdist) == 1, (
            f"expected 1 wheel + 1 sdist, got {len(whl)}/{len(sdist)}"
        )

    @pytest.mark.slow
    def test_twine_check_passes(self, tmp_path):
        # Build first.
        subprocess.check_call(
            [sys.executable, "-m", "venv", str(tmp_path / "venv")],
            cwd=ROOT,
        )
        venv_py = tmp_path / "venv" / "bin" / "python"
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install", "--quiet", "build", "twine"],
            cwd=ROOT,
        )
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install", "--quiet", "-e", "."],
            cwd=ROOT,
        )
        subprocess.check_call(
            [str(venv_py), "-m", "build", "--outdir", str(tmp_path / "dist")],
            cwd=ROOT,
        )
        result = subprocess.run(
            [str(venv_py), "-m", "twine", "check", str(tmp_path / "dist") + "/*"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"twine check failed:\n{result.stdout}\n{result.stderr}"
        assert "PASSED" in result.stdout

    @pytest.mark.slow
    def test_built_wheel_contains_all_modules(self, tmp_path):
        import zipfile

        subprocess.check_call(
            [sys.executable, "-m", "venv", str(tmp_path / "venv")],
            cwd=ROOT,
        )
        venv_py = tmp_path / "venv" / "bin" / "python"
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install", "--quiet", "build"],
            cwd=ROOT,
        )
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install", "--quiet", "-e", "."],
            cwd=ROOT,
        )
        subprocess.check_call(
            [str(venv_py), "-m", "build", "--wheel", "--outdir", str(tmp_path / "dist")],
            cwd=ROOT,
        )
        whl = next((tmp_path / "dist").glob("*.whl"))
        with zipfile.ZipFile(whl) as zf:
            names = zf.namelist()
        # All our modules must ship in the wheel.
        expected = [
            "tg_cli/__init__.py",
            "tg_cli/cli/main.py",
            "tg_cli/cli/_output.py",
            "tg_cli/client.py",
            "tg_cli/config.py",
            "tg_cli/daemon.py",
            "tg_cli/db.py",
            "tg_cli/queue.py",
            "tg_cli/ratelimit.py",
            "tg_cli/throttle.py",
            "tg_cli/console.py",
            "tg_cli/cli/_chat.py",
            "tg_cli/cli/_sync.py",
            "tg_cli/cli/data.py",
            "tg_cli/cli/query.py",
            "tg_cli/cli/tg.py",
        ]
        for m in expected:
            assert m in names, f"wheel missing module: {m}"

    @pytest.mark.slow
    def test_sdist_contains_package_and_readme(self, tmp_path):
        import tarfile

        subprocess.check_call(
            [sys.executable, "-m", "venv", str(tmp_path / "venv")],
            cwd=ROOT,
        )
        venv_py = tmp_path / "venv" / "bin" / "python"
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install", "--quiet", "build"],
            cwd=ROOT,
        )
        subprocess.check_call(
            [str(venv_py), "-m", "pip", "install", "--quiet", "-e", "."],
            cwd=ROOT,
        )
        subprocess.check_call(
            [str(venv_py), "-m", "build", "--sdist", "--outdir", str(tmp_path / "dist")],
            cwd=ROOT,
        )
        sdist = next((tmp_path / "dist").glob("*.tar.gz"))
        with tarfile.open(sdist) as tf:
            names = tf.getnames()
        # pyproject.toml must be at the root.
        assert any(n.endswith("/pyproject.toml") for n in names), (
            "sdist missing pyproject.toml"
        )
        # The src package must be present.
        assert any("src/tg_cli" in n for n in names), "sdist missing src/tg_cli"


# ─────────────────────────── Publish flow validation ───────────────────────────


class TestPublishFlow:
    def test_publish_job_requires_verify(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        needs = pub["jobs"]["publish"].get("needs")
        assert "verify" in needs, "publish must wait for the verify (ci.yml) job"

    def test_publish_uses_pypi_environment(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        env = pub["jobs"]["publish"].get("environment")
        assert env == "pypi", (
            "publish must use a 'pypi' environment to bind to Trusted Publisher config"
        )

    def test_publish_does_not_skip_publish_when_verify_fails(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        # Default for `needs` is success; make sure it's not overridden.
        assert "if" not in pub["jobs"]["publish"]

    def test_publish_job_runs_on_latest_ubuntu(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        runner = pub["jobs"]["publish"]["runs-on"]
        assert "ubuntu" in runner

    def test_publish_steps_have_unique_names(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        names = [s.get("name") for s in pub["jobs"]["publish"]["steps"] if s.get("name")]
        assert len(names) == len(set(names)), f"duplicate step names: {names}"

    def test_publish_builds_before_publishing(self):
        with open(WORKFLOWS / "publish.yml") as f:
            pub = yaml.safe_load(f)
        steps = pub["jobs"]["publish"]["steps"]
        build_idx = next(
            i for i, s in enumerate(steps) if "Build" in s.get("name", "")
        )
        publish_idx = next(
            i for i, s in enumerate(steps) if "pypa/gh-action-pypi-publish" in s.get("uses", "")
        )
        assert build_idx < publish_idx, "build must come before publish step"


# ─────────────────────────── Reproduce the matrix locally ───────────────────────────


class TestMatrixLocally:
    """One Python version is tested in CI per matrix entry; verify pytest passes
    on the system Python (the most common local dev path).

    The subprocess test is opt-in: ``uv run pytest`` from inside pytest is
    a slow recursion. Run with ``pytest -m slow``.
    """

    @pytest.mark.slow
    def test_pytest_passes_quickly(self):
        # Exclude this file to avoid infinite recursion.
        result = subprocess.run(
            ["uv", "run", "pytest", "-q", "--no-header", "-x",
             "--ignore=tests/test_workflows.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
        assert result.returncode == 0, (
            f"pytest failed:\n{result.stdout[-2000:]}\n{result.stderr[-1000:]}"
        )

    def test_ruff_passes(self):
        result = subprocess.run(
            ["uv", "run", "ruff", "check", "."],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"ruff failed:\n{result.stdout}\n{result.stderr}"
        )
