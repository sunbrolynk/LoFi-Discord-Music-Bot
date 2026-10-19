"""Tests for the changelog gate: a source change needs an entry or a `Changelog: none` trailer."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-changelog.sh"
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=ENV, capture_output=True)  # noqa: S603, S607


def commit(repo: Path, path: str, message: str) -> None:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(file.read_text() + "x\n" if file.exists() else "x\n")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    commit(tmp_path, "README.md", "base")
    git(tmp_path, "tag", "base")
    return tmp_path


def check(repo: Path) -> int:
    cmd = ["bash", str(SCRIPT), "base..HEAD"]
    return subprocess.run(cmd, cwd=repo, env=ENV, capture_output=True).returncode  # noqa: S603, S607


def test_source_change_without_entry_fails(repo: Path) -> None:
    commit(repo, "src/bot.py", "change the bot")
    commit(repo, "README.md", "docs")
    assert check(repo) != 0


def test_later_entry_covers_earlier_source_change(repo: Path) -> None:
    commit(repo, "src/bot.py", "change the bot")
    commit(repo, "CHANGELOG.md", "record it")
    assert check(repo) == 0


def test_entry_before_the_change_does_not_cover_it(repo: Path) -> None:
    commit(repo, "CHANGELOG.md", "record it")
    commit(repo, "src/bot.py", "change the bot")
    assert check(repo) != 0


def test_changelog_none_trailer_opts_out(repo: Path) -> None:
    commit(repo, "src/bot.py", "tidy the bot\n\nChangelog: none")
    assert check(repo) == 0


def test_non_source_change_needs_no_entry(repo: Path) -> None:
    commit(repo, "tests/test_x.py", "add a test")
    assert check(repo) == 0
