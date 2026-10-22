"""
Tests for `claudepath install` / `uninstall` and the shell block itself,
including running the block in real zsh and bash.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claudepath import shellrc
from claudepath.cli import cmd_install, cmd_status, cmd_uninstall
from claudepath.registry import load_registry

USER_WRAPPER = 'claude() { caffeinate -d claude "$@"; }'


# ─── Block editing ─────────────────────────────────────────────────────────

def test_with_block_appends_once_and_updates_in_place():
    text = "export PATH=x\n" + USER_WRAPPER + "\n"
    once = shellrc.with_block(text, "zsh")
    assert once.startswith(text + "\n" + shellrc.BEGIN)
    assert shellrc.with_block(once, "zsh") == once
    stale = once.replace("claudepath auto || true", "claudepath auto")
    assert shellrc.with_block(stale, "zsh") == once
    assert shellrc.without_block(once) == text
    assert shellrc.without_block(text) is None


def test_wrappers_before_and_after_block():
    with_block = shellrc.with_block(USER_WRAPPER + "\n", "zsh")
    assert shellrc.wrappers_outside_block(with_block) == (True, False)
    later = with_block + "alias claude='claude --x'\n"
    assert shellrc.wrappers_outside_block(later) == (True, True)


def test_detect_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert shellrc.detect_shell() == "zsh"
    assert shellrc.detect_shell("bash") == "bash"
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    with pytest.raises(ValueError):
        shellrc.detect_shell()


# ─── install / uninstall commands ──────────────────────────────────────────

@pytest.fixture
def rc(env, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("claudepath.cli.find_claude_dir", lambda: env.claude_dir)
    path = env.home / ".zshrc"
    path.write_text('export PATH="$HOME/.local/bin:$PATH"\n' + USER_WRAPPER + "\n")
    os.chmod(path, 0o644)
    return path


def test_install_dry_run_writes_nothing(env, rc, capsys):
    before = rc.read_text()
    cmd_install(["--dry-run"])
    assert rc.read_text() == before
    assert not (env.claude_dir / "claudepath").exists()
    out = capsys.readouterr().out
    assert shellrc.BEGIN in out and "existing claude function" in out


def test_install_asks_backs_up_and_syncs(env, rc, monkeypatch, capsys):
    project = env.add_project("p", sessions=1)
    original = rc.read_text()
    monkeypatch.setattr("builtins.input", lambda _: "n")
    cmd_install([])
    assert rc.read_text() == original

    monkeypatch.setattr("builtins.input", lambda _: "y")
    cmd_install([])
    text = rc.read_text()
    assert text.startswith(original) and text.count(shellrc.BEGIN) == 1
    assert oct(rc.stat().st_mode & 0o777) == oct(0o644)
    assert project in load_registry(env.claude_dir)["projects"]
    backups = list((env.claude_dir / "claudepath" / "backups").glob("*/files/*-.zshrc"))
    assert len(backups) == 1 and backups[0].read_text() == original

    cmd_install(["--yes"])
    assert rc.read_text() == text
    assert "already installed" in capsys.readouterr().out


def test_uninstall_restores_the_original_file(env, rc):
    original = rc.read_text()
    cmd_install(["--yes"])
    cmd_uninstall(["--yes"])
    assert rc.read_text() == original
    cmd_uninstall(["--yes"])  # nothing left: no-op


def test_status_reports_hook_and_encoding(env, rc, monkeypatch, capsys):
    env.add_project("p", sessions=1)
    monkeypatch.setattr("claudepath.cli._claude_code_version", lambda: "2.1.308 (Claude Code)")
    cmd_status([])
    out = capsys.readouterr().out
    assert "not installed" in out and "1 project dir(s) match exactly" in out
    cmd_install(["--yes"])
    capsys.readouterr()
    cmd_status([])
    out = capsys.readouterr().out
    assert "installed in" in out and "chains your existing claude wrapper" in out


# ─── The block in real shells ──────────────────────────────────────────────

def _fake_tools(bindir: Path) -> None:
    bindir.mkdir()
    tools = {
        "claude": 'echo "BIN claude argc=$# [$*]" >> "$LOG"; exit 7',
        "claudepath": 'echo "claudepath $*" >> "$LOG"; exit 1',
        "caffeinate": 'echo "caffeinate $*" >> "$LOG"; shift; exec "$@"',
    }
    for name, body in tools.items():
        f = bindir / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(0o755)


def _run_shell(tmp_path, shell: str, prelude: str, call: str = 'claude "a b" c') -> list:
    _fake_tools(tmp_path / "bin")
    rc = tmp_path / "rc"
    rc.write_text(prelude + "\n" + shellrc.render_block(shell))
    log = tmp_path / "log"
    log.write_text("")
    script = f'. ./rc; . ./rc; {call}; echo "exit=$?" >> "$LOG"'
    if shell == "zsh":
        cmd = ["zsh", "-f", "-c", script]
    else:
        cmd = ["bash", "--norc", "--noprofile", "-c", "shopt -s expand_aliases\n" + script]
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "LOG": str(log), "HOME": str(tmp_path)}
    subprocess.run(cmd, cwd=tmp_path, env=env, check=True, timeout=20)
    return log.read_text().splitlines()


SHELLS = [s for s in ("zsh", "bash") if shutil.which(s)]


@pytest.mark.parametrize("shell", SHELLS)
def test_real_shell_without_existing_wrapper(tmp_path, shell):
    assert _run_shell(tmp_path, shell, "") == [
        "claudepath auto", "BIN claude argc=2 [a b c]", "exit=7"]


@pytest.mark.parametrize("shell", SHELLS)
def test_real_shell_keeps_users_caffeinate_wrapper(tmp_path, shell):
    assert _run_shell(tmp_path, shell, USER_WRAPPER) == [
        "claudepath auto", "caffeinate -d claude a b c", "BIN claude argc=2 [a b c]", "exit=7"]


@pytest.mark.parametrize("shell", SHELLS)
def test_real_shell_self_referencing_alias_does_not_recurse(tmp_path, shell):
    assert _run_shell(tmp_path, shell, "alias claude='claude --dangerously-skip-permissions'") == [
        "claudepath auto", "BIN claude argc=3 [--dangerously-skip-permissions a b c]", "exit=7"]


@pytest.mark.parametrize("shell", SHELLS)
def test_real_shell_without_claudepath_on_path(tmp_path, shell):
    lines = _run_shell(tmp_path, shell, USER_WRAPPER,
                       call='rm -f bin/claudepath; claude x')
    assert lines == ["caffeinate -d claude x", "BIN claude argc=1 [x]", "exit=7"]


def test_damaged_markers_are_refused(env, rc, capsys):
    text = rc.read_text() + shellrc.BEGIN + "\nfunction claude { :; }\n"  # END was deleted
    rc.write_text(text)
    with pytest.raises(SystemExit):
        cmd_install(["--yes"])
    with pytest.raises(SystemExit):
        cmd_uninstall(["--yes"])
    assert rc.read_text() == text
    assert "without" in capsys.readouterr().err
