"""Tests for the lock audit: only advisories a change introduces fail it."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "audit-deps.sh"

# Stands in for pip-audit: "name==version # vuln ID" reports that advisory, "# skip" marks the
# package unauditable, and a "crash" line fails.
FAKE_PIP_AUDIT = """\
#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
lock, out = args[args.index("-r") + 1], args[args.index("-o") + 1]
deps = []
for line in open(lock):
    if line.strip() == "crash":
        sys.exit(2)
    if line.rstrip().endswith("# skip"):
        deps.append({"name": line.partition("==")[0], "skip_reason": "not on PyPI"})
        continue
    spec, _, vuln = line.partition("# vuln ")
    name, _, version = spec.strip().partition("==")
    if name:
        deps.append({"name": name, "version": version, "vulns": [{"id": vuln.strip()}] if vuln else []})
json.dump({"dependencies": deps}, open(out, "w"))
sys.exit(1 if any(d.get("vulns") or "skip_reason" in d for d in deps) else 0)
"""


ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=ENV, capture_output=True)  # noqa: S603, S607


def env_for(repo: Path) -> dict[str, str]:
    env = dict(ENV)
    env["PATH"] = f"{repo / 'bin'}{os.pathsep}{env['PATH']}"
    env["LOFI_AUDIT_BASE"] = "base"
    return env


def write_locks(repo: Path, runtime: str, dev: str = "ok==1.0\n") -> None:
    (repo / "requirements.txt").write_text(runtime)
    (repo / "requirements-dev.txt").write_text(dev)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    fake = tmp_path / "bin" / "pip-audit"
    fake.parent.mkdir()
    fake.write_text(FAKE_PIP_AUDIT)
    fake.chmod(0o755)
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    write_locks(tmp_path, "old==1.0 # vuln OLD-1\n")
    git(tmp_path, "add", "requirements.txt", "requirements-dev.txt")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "tag", "base")
    return tmp_path


def audit(repo: Path) -> subprocess.CompletedProcess[str]:
    cmd = ["bash", str(SCRIPT)]
    return subprocess.run(cmd, cwd=repo, env=env_for(repo), capture_output=True, text=True)  # noqa: S603, S607


def test_advisory_already_on_base_does_not_block(repo: Path) -> None:
    result = audit(repo)
    assert result.returncode == 0
    assert "old==1.0 OLD-1" in result.stdout


def test_new_advisory_blocks(repo: Path) -> None:
    write_locks(repo, "old==1.0 # vuln OLD-1\nnew==2.0 # vuln NEW-1\n")
    result = audit(repo)
    assert result.returncode == 1
    assert "new==2.0 NEW-1" in result.stderr


def test_same_package_at_a_new_vulnerable_version_blocks(repo: Path) -> None:
    write_locks(repo, "old==1.1 # vuln OLD-1\n")
    assert audit(repo).returncode == 1


def test_advisory_in_a_lock_missing_from_base_blocks(repo: Path) -> None:
    git(repo, "rm", "-q", "--cached", "requirements-dev.txt")
    git(repo, "commit", "-q", "-m", "drop dev lock")
    git(repo, "tag", "-f", "base")
    write_locks(repo, "old==1.0 # vuln OLD-1\n", dev="dev==1.0 # vuln DEV-1\n")
    assert audit(repo).returncode == 1


def test_pip_audit_crash_fails(repo: Path) -> None:
    write_locks(repo, "crash\n")
    assert audit(repo).returncode != 0


def test_newly_unauditable_package_blocks(repo: Path) -> None:
    write_locks(repo, "old==1.0 # vuln OLD-1\nlocal==1.0 # skip\n")
    result = audit(repo)
    assert result.returncode == 1
    assert "local skipped" in result.stderr


def test_package_already_unauditable_on_base_does_not_block(repo: Path) -> None:
    write_locks(repo, "old==1.0 # vuln OLD-1\nlocal==1.0 # skip\n")
    git(repo, "commit", "-q", "-am", "add local")
    git(repo, "tag", "-f", "base")
    assert audit(repo).returncode == 0
