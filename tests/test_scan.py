"""
Tests for `claudepath scan` (rescuing already-orphaned sessions) and the
orphan helpers it shares with `claudepath status`.
"""

import os
import shutil
import sys

import pytest

from claudepath.cli import cmd_scan
from claudepath.orphans import find_orphans, index_tree, match_orphans
from claudepath.registry import claude_project_path, load_registry
from tests.conftest import init_git, needs_git


@pytest.fixture(autouse=True)
def _no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("claudepath.cli.find_claude_dir", lambda: None)  # set per test


def scan(env, monkeypatch, *args):
    monkeypatch.setattr("claudepath.cli.find_claude_dir", lambda: env.claude_dir)
    cmd_scan([str(env.work), *args])


def test_orphans_are_dirs_whose_folder_is_gone(env):
    keep = env.add_project("keep", sessions=1)
    lost = env.add_project("lost", sessions=2)
    shutil.rmtree(lost)
    orphans, unknown = find_orphans(env.claude_dir, env.claude_json, load_registry(env.claude_dir))
    assert [(o.path, o.sessions) for o in orphans] == [(lost, 2)]
    assert unknown == []
    assert keep not in [o.path for o in orphans]


def test_registry_inode_match_moves_without_asking(env, monkeypatch, capsys):
    old = env.add_project("deep/app", sessions=1)
    env.sync()
    target = env.work / "elsewhere" / "renamed-app"
    target.parent.mkdir()
    os.rename(old, target)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("asked for an inode match"))
    scan(env, monkeypatch)
    new = claude_project_path(str(target))
    assert env.project_dir(new).is_dir() and not env.project_dir(old).exists()
    assert "1 project(s) rescued" in capsys.readouterr().out


def test_unique_name_match_asks_first(env, monkeypatch):
    old = env.add_project("site", sessions=1)
    shutil.rmtree(old)  # moved before claudepath knew it: no registry entry
    (env.work / "clients" / "site").mkdir(parents=True)
    new = claude_project_path(str(env.work / "clients" / "site"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    monkeypatch.setattr("builtins.input", lambda _: "n")
    scan(env, monkeypatch)
    assert env.project_dir(old).is_dir()

    monkeypatch.setattr("builtins.input", lambda _: "y")
    scan(env, monkeypatch)
    assert env.project_dir(new).is_dir() and not env.project_dir(old).exists()


def test_name_match_with_yes_and_without_tty(env, monkeypatch, capsys):
    old = env.add_project("tool", sessions=1)
    shutil.rmtree(old)
    (env.work / "moved" / "tool").mkdir(parents=True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    scan(env, monkeypatch)
    assert env.project_dir(old).is_dir()
    assert "confirm in a terminal or pass --yes" in capsys.readouterr().out
    scan(env, monkeypatch, "--yes")
    assert not env.project_dir(old).exists()


def test_ambiguous_names_and_dry_run_change_nothing(env, monkeypatch, capsys):
    old = env.add_project("api", sessions=1)
    shutil.rmtree(old)
    (env.work / "a" / "api").mkdir(parents=True)
    (env.work / "b" / "api").mkdir(parents=True)
    other = env.add_project("web", sessions=1)
    env.sync()
    os.rename(other, env.work / "web2")
    snap = env.snapshot()
    scan(env, monkeypatch, "--dry-run", "--yes")
    assert env.snapshot() == snap
    out = capsys.readouterr().out
    assert "2 folders named 'api'; ambiguous" in out
    assert "web2" in out and "same folder (inode)" in out


@needs_git
def test_name_match_rejected_by_different_git_history(env, monkeypatch, capsys):
    old = env.add_project("lib", sessions=1, git=True)
    env.sync()
    shutil.rmtree(old)
    impostor = env.work / "other" / "lib"
    impostor.mkdir(parents=True)
    init_git(impostor)  # a different repository with the same folder name
    scan(env, monkeypatch, "--yes")
    assert env.project_dir(old).is_dir()
    assert "different git history" in capsys.readouterr().out


def test_subfolder_orphan_rides_along_with_its_parent(env, monkeypatch):
    old = env.add_project("mono", sessions=1)
    env.add_sessions(old + "/sub", 1)
    env.sync()
    os.rename(old, env.work / "mono-renamed")
    reg = load_registry(env.claude_dir)
    orphans, _ = find_orphans(env.claude_dir, env.claude_json, reg)
    by_inode, by_name = index_tree(str(env.work), env.claude_dir)
    matches, unmatched = match_orphans(orphans, by_inode, by_name)
    assert [m.orphan.path for m in matches] == [old]
    scan(env, monkeypatch)
    new = claude_project_path(str(env.work / "mono-renamed"))
    assert env.project_dir(new + "/sub").is_dir()


def test_index_tree_skips_dependency_folders(env):
    (env.work / "p" / "node_modules" / "deep" / "x").mkdir(parents=True)
    (env.work / "p" / "src").mkdir(parents=True)
    _, by_name = index_tree(str(env.work), env.claude_dir)
    assert "src" in by_name and "deep" not in by_name and "node_modules" not in by_name


def test_unconfirmed_inode_match_asks_first(env, monkeypatch, capsys):
    import json as _json

    from claudepath.registry import registry_path

    old = env.add_project("linuxish", sessions=1)
    env.sync()
    data = _json.loads(registry_path(env.claude_dir).read_text())
    data["projects"][old].pop("birth", None)  # as recorded on Linux
    registry_path(env.claude_dir).write_text(_json.dumps(data))
    os.rename(old, env.work / "moved-linuxish")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    scan(env, monkeypatch)
    assert env.project_dir(old).is_dir()
    assert "nothing to confirm it" in capsys.readouterr().out
