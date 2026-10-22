"""
End-to-end tests for `claudepath auto` in a fake HOME: renames, moves, merges,
subfolder projects, cross-volume (weak) matches, dry runs and rollback.
"""

import io
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from claudepath import auto
from claudepath.backup import list_journals, get_journal_base, restore_journal
from claudepath.encoder import encode_path
from claudepath.registry import claude_project_path, load_registry, registry_path
from tests.conftest import needs_git


def run_auto(env, cwd, monkeypatch, **kw) -> int:
    monkeypatch.chdir(cwd)
    return auto.run(claude_dir=env.claude_dir, **kw)


def run_hook(env, cwd, monkeypatch) -> int:
    """Like the shell hook: through auto.main(), which reports errors and never raises."""
    monkeypatch.chdir(cwd)
    return auto.main(["--claude-dir", str(env.claude_dir)])


def renamed(env, path: str, new_rel: str) -> str:
    target = env.work / new_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(path, target)
    return claude_project_path(str(target))


def assert_only_cwd_changed(before: bytes, after: bytes, old: str, new: str) -> None:
    """Every line is byte-identical, or differs only in its top-level cwd."""
    b_lines, a_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    assert len(b_lines) == len(a_lines)
    for b, a in zip(b_lines, a_lines):
        if a == b:
            continue
        ob, oa = json.loads(b), json.loads(a)
        assert ob["cwd"] == old or ob["cwd"].startswith(old + "/")
        ob["cwd"] = new + ob["cwd"][len(old):]
        assert oa == ob


# ─── Strong match: rename / move ───────────────────────────────────────────

def test_rename_migrates_sessions_history_settings_and_registry(env, monkeypatch, capsys):
    old = env.add_project("app", sessions=2)
    env.sync()
    old_dir = env.project_dir(old)
    files_before = {p.relative_to(old_dir): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                    for p in old_dir.rglob("*") if p.is_file()}
    history_before = env.history_lines()
    config_mode = env.claude_json.stat().st_mode

    new = renamed(env, old, "app-renamed")
    assert run_auto(env, new, monkeypatch) == 0

    new_dir = env.project_dir(new)
    assert not old_dir.exists() and new_dir.is_dir()
    for rel, (data, mtime, mode) in files_before.items():
        f = new_dir / rel
        assert f.stat().st_mtime_ns == mtime, rel  # --resume ordering preserved
        assert f.stat().st_mode == mode
        if f.suffix == ".jsonl":
            assert_only_cwd_changed(data, f.read_bytes(), old, new)
        else:
            assert f.read_bytes() == data  # memory, tool results: untouched
    # The nested "cwd" keys and the sibling path stayed as they were.
    some_session = next(new_dir.glob("*.jsonl")).read_text()
    assert f'"input":{{"command":"ls","cwd":"{old}"' in some_session
    assert f'"cwd":"{old}-other"' in some_session
    assert f'"cwd":"{new}/sub"' in some_session

    history_after = env.history_lines()
    for b, a in zip(history_before, history_after):
        ob, oa = json.loads(b), json.loads(a)
        ob["project"] = new if ob["project"] == old else ob["project"]
        assert oa == ob

    config = env.read_config()
    assert old not in config["projects"]
    assert config["projects"][new]["allowedTools"] == ["Bash(npm test)"]
    assert config["projects"]["/somewhere/else"]["hasTrustDialogAccepted"] is False
    assert config["oauthAccount"] == {"emailAddress": "someone@example.com"}
    assert env.claude_json.stat().st_mode == config_mode  # 0600 kept

    reg = load_registry(env.claude_dir)
    assert old not in reg["projects"] and new in reg["projects"]
    assert reg["projects"][new]["ino"] == os.stat(new).st_ino

    err = capsys.readouterr().err
    assert "sessions followed your folder" in err and "2 sessions" in err


def test_move_into_another_parent(env, monkeypatch):
    old = env.add_project("clients/acme/site", sessions=1)
    env.sync()
    new = renamed(env, old, "archive/2026/site")
    run_auto(env, new, monkeypatch)
    assert env.project_dir(new).is_dir()
    assert not env.project_dir(old).exists()
    assert new in env.read_config()["projects"]


def test_subfolder_projects_and_settings_follow(env, monkeypatch):
    old = env.add_project("mono", sessions=1)
    env.add_sessions(old + "/sub", 1, memory="sub notes\n")
    env.config["githubRepoPaths"]["me/mono"] = [old, "/x/y"]
    env.flush()
    env.sync()

    new = renamed(env, old, "monorepo")
    run_auto(env, new, monkeypatch)

    sub_dir = env.project_dir(new + "/sub")
    assert sub_dir.is_dir() and not env.project_dir(old + "/sub").exists()
    assert (sub_dir / "memory" / "MEMORY.md").read_text() == "sub notes\n"
    assert f'"cwd":"{new}/sub"' in next(sub_dir.glob("*.jsonl")).read_text()
    config = env.read_config()
    assert new + "/sub" in config["projects"] and old + "/sub" not in config["projects"]
    assert config["githubRepoPaths"]["me/mono"] == [new, "/x/y"]


def test_launch_from_a_subfolder_of_the_moved_project(env, monkeypatch):
    old = env.add_project("proj", sessions=1)
    env.sync()
    new = renamed(env, old, "proj2")
    run_auto(env, Path(new) / "sub", monkeypatch)
    assert env.project_dir(new).is_dir() and not env.project_dir(old).exists()


def test_merge_into_existing_new_project_dir(env, monkeypatch, capsys):
    old = env.add_project("site", sessions=2, memory="old notes\n")
    env.sync()
    new = renamed(env, old, "site-v2")
    # Claude Code was already used at the new path (e.g. from an IDE) before auto ran.
    new_ids = env.add_sessions(new, 1, memory="new notes\n", settings={
        "allowedTools": [], "hasTrustDialogAccepted": True, "onlyInNew": 1})
    shared = env.project_dir(old) / "same.txt"
    shared.write_text("identical\n")
    (env.project_dir(new) / "same.txt").write_text("identical\n")
    old_sessions = {p.name for p in env.project_dir(old).glob("*.jsonl")}

    run_auto(env, new, monkeypatch)

    new_dir = env.project_dir(new)
    assert not env.project_dir(old).exists()
    names = {p.name for p in new_dir.glob("*.jsonl")}
    assert old_sessions | {f"{new_ids[0]}.jsonl"} <= names
    assert (new_dir / "memory" / "MEMORY.md").read_text() == "new notes\n"  # not clobbered
    conflicts = list((new_dir / "memory").glob("MEMORY.claudepath-conflict-*.md"))
    assert len(conflicts) == 1 and conflicts[0].read_text() == "old notes\n"
    assert (new_dir / "same.txt").read_text() == "identical\n"
    assert not list(new_dir.glob("same.claudepath-conflict-*"))
    # Settings merged: old values win, new-only keys survive.
    merged = env.read_config()["projects"][new]
    assert merged["allowedTools"] == ["Bash(npm test)"] and merged["onlyInNew"] == 1
    assert old not in env.read_config()["projects"]
    assert "kept side by side" in capsys.readouterr().err


def test_old_path_reused_by_a_new_folder(env, monkeypatch):
    old = env.add_project("svc", sessions=1)
    env.sync()
    new = renamed(env, old, "svc-legacy")
    os.makedirs(old)  # a fresh, unrelated folder at the old path
    run_auto(env, new, monkeypatch)
    assert env.project_dir(new).is_dir() and not env.project_dir(old).exists()


def test_case_only_rename(env, monkeypatch):
    old = env.add_project("CaseProj", sessions=1)
    env.sync()
    if not os.path.exists(old.replace("CaseProj", "caseproj")):
        pytest.skip("case-sensitive filesystem: a case-only rename is an ordinary rename")
    tmp = env.work / "tmp-name"
    os.rename(old, tmp)
    os.rename(tmp, env.work / "caseproj")
    new = claude_project_path(str(env.work / "caseproj"))
    run_auto(env, new, monkeypatch)
    # On a case-insensitive volume both spellings resolve, so look at the
    # names actually stored on disk.
    names = os.listdir(env.claude_dir / "projects")
    assert encode_path(new) in names and encode_path(old) not in names
    lines = next(env.project_dir(new).glob("*.jsonl")).read_text().splitlines()
    top_level = {json.loads(l).get("cwd") for l in lines} - {None}
    assert top_level == {new, new + "/sub", old + "-other"}
    assert new in env.read_config()["projects"] and old not in env.read_config()["projects"]

    backup = list_journals(get_journal_base(env.claude_dir))[0]["path"]
    from claudepath.migrate import undo_migration

    undo_migration(backup, env.claude_dir)
    assert encode_path(old) in os.listdir(env.claude_dir / "projects")


# ─── Safety ────────────────────────────────────────────────────────────────

def test_no_identity_match_changes_nothing(env, monkeypatch):
    old = env.add_project("gone-project", sessions=1)
    env.sync()
    shutil.rmtree(old)  # deleted, not moved
    other = env.work / "brand-new"
    other.mkdir()
    snap = env.snapshot()
    run_auto(env, other, monkeypatch)
    assert env.snapshot() == snap
    assert claude_project_path(str(other)) in load_registry(env.claude_dir)["projects"]


def test_birth_time_mismatch_blocks_a_recycled_inode(env, monkeypatch):
    old = env.add_project("p", sessions=1)
    env.sync()
    reg_file = registry_path(env.claude_dir)
    data = json.loads(reg_file.read_text())
    data["projects"][old]["birth"] = 12345.0  # a different folder had this inode
    reg_file.write_text(json.dumps(data))
    new = renamed(env, old, "p2")
    if getattr(os.stat(new), "st_birthtime", None) is None:
        pytest.skip("no birth time on this platform")
    snap = env.snapshot()
    run_auto(env, new, monkeypatch)
    assert env.snapshot() == snap


def test_dry_run_writes_nothing(env, monkeypatch, capsys):
    old = env.add_project("dry", sessions=1)
    env.sync()
    new = renamed(env, old, "dry2")
    reg_before = registry_path(env.claude_dir).read_bytes()
    snap = env.snapshot()
    run_auto(env, new, monkeypatch, dry_run=True)
    assert env.snapshot() == snap
    assert registry_path(env.claude_dir).read_bytes() == reg_before
    assert not get_journal_base(env.claude_dir).exists()
    err = capsys.readouterr().err
    assert "[dry run] would move sessions" in err and "updated cwd in" in err


def test_fast_path_runs_no_subprocess_and_writes_nothing(env, monkeypatch):
    path = env.add_project("steady", sessions=1, git=False)
    env.sync()
    run_auto(env, path, monkeypatch)  # first launch records git info
    reg_file = registry_path(env.claude_dir)
    stamp = reg_file.stat().st_mtime_ns

    import subprocess

    def boom(*a, **k):
        raise AssertionError("subprocess called on the fast path")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr("claudepath.registry.registry_lock", boom)
    run_auto(env, path, monkeypatch)
    assert reg_file.stat().st_mtime_ns == stamp


def test_running_session_in_moved_folder_defers(env, monkeypatch, capsys):
    old = env.add_project("busy", sessions=1)
    env.sync()
    new = renamed(env, old, "busy2")
    monkeypatch.setattr(auto, "_claude_processes_in", lambda *paths: [4242])
    snap = env.snapshot()
    run_auto(env, new, monkeypatch)
    assert env.snapshot() == snap
    assert "still running" in capsys.readouterr().err
    assert old in load_registry(env.claude_dir)["projects"]  # retried next launch


def test_errors_never_block_the_launch(env, monkeypatch, capsys):
    old = env.add_project("err", sessions=1)
    env.sync()
    new = renamed(env, old, "err2")
    monkeypatch.chdir(new)

    def explode(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("claudepath.migrate.migrate", explode)
    assert auto.main(["--claude-dir", str(env.claude_dir)]) == 1
    assert "auto skipped: disk on fire" in capsys.readouterr().err


def test_failed_migration_rolls_everything_back(env, monkeypatch):
    old = env.add_project("rb", sessions=2)
    env.add_sessions(old + "/sub", 1)
    env.sync()
    new = renamed(env, old, "rb2")
    snap = env.snapshot()
    registry_before = registry_path(env.claude_dir).read_bytes()

    def fail(*a, **k):
        raise OSError("simulated failure after sessions and history were rewritten")

    monkeypatch.setattr("claudepath.migrate.update_claude_json", fail)
    monkeypatch.chdir(new)
    assert auto.main(["--claude-dir", str(env.claude_dir)]) == 1
    assert env.snapshot() == snap  # bytes and mtimes, every file
    assert registry_path(env.claude_dir).read_bytes() == registry_before
    journals = list_journals(get_journal_base(env.claude_dir))
    assert journals[0]["status"] == "rolled back"


def test_backup_restores_the_pre_migration_state(env, monkeypatch):
    old = env.add_project("restore-me", sessions=2)
    env.add_sessions(old + "/sub", 1)
    env.sync()
    snap = env.snapshot()
    new = renamed(env, old, "restored")
    run_auto(env, new, monkeypatch)
    assert env.snapshot() != snap

    backup = list_journals(get_journal_base(env.claude_dir))[0]["path"]
    from claudepath.migrate import undo_migration

    assert "completed migration" in restore_journal(backup)[0]  # the low-level copy refuses
    undo_migration(backup, env.claude_dir)
    after = env.snapshot()
    assert {k: v[0] for k, v in after.items()} == {k: v[0] for k, v in snap.items()}  # bytes
    assert {k: v[1] for k, v in after.items() if k.endswith(".jsonl")} == \
        {k: v[1] for k, v in snap.items() if k.endswith(".jsonl")}  # session mtimes
    assert not env.project_dir(new).exists()


def test_first_run_without_registry_records_projects(env, monkeypatch):
    a = env.add_project("a", sessions=1)
    b = env.add_project("b", sessions=1)
    run_auto(env, a, monkeypatch)
    projects = load_registry(env.claude_dir)["projects"]
    assert a in projects and b in projects


# ─── Weak match: cross-volume copy (new inode, same git history) ──────────

def _copy_across_volumes(env, old: str, new_rel: str) -> str:
    target = env.work / new_rel
    shutil.copytree(old, target, symlinks=True)
    shutil.rmtree(old)
    return claude_project_path(str(target))


@needs_git
def test_weak_match_moves_after_yes(env, monkeypatch, capsys):
    old = env.add_project("repo", sessions=1, git=True, remote="git@github.com:me/repo.git")
    env.sync()
    new = _copy_across_volumes(env, old, "external/repo")
    assert os.stat(new).st_ino != load_registry(env.claude_dir)["projects"][old]["ino"]
    monkeypatch.setattr(auto, "_interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    run_auto(env, new, monkeypatch)
    assert env.project_dir(new).is_dir() and not env.project_dir(old).exists()
    assert "same git history" in capsys.readouterr().err
    assert env.read_config()["githubRepoPaths"]["me/repo.git"] == [new]


@needs_git
def test_weak_match_no_is_remembered(env, monkeypatch):
    old = env.add_project("repo", sessions=1, git=True)
    env.sync()
    new = _copy_across_volumes(env, old, "copy/repo")
    monkeypatch.setattr(auto, "_interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    snap = env.snapshot()
    run_auto(env, new, monkeypatch)
    assert env.snapshot() == snap
    assert load_registry(env.claude_dir)["declined"][0]["from"] == old

    class NoInput:
        def readline(self):
            raise AssertionError("asked again after a no")

    monkeypatch.setattr(sys, "stdin", NoInput())
    run_auto(env, new, monkeypatch)
    assert env.snapshot() == snap


@needs_git
def test_weak_match_never_prompts_without_a_tty(env, monkeypatch):
    old = env.add_project("repo", sessions=1, git=True)
    env.sync()
    new = _copy_across_volumes(env, old, "ci/repo")
    monkeypatch.setattr(auto, "_interactive", lambda: False)
    snap = env.snapshot()
    run_auto(env, new, monkeypatch)
    assert env.snapshot() == snap
    assert load_registry(env.claude_dir)["declined"] == []


@needs_git
def test_weak_match_needs_exactly_one_candidate(env, monkeypatch):
    first = env.add_project("one/repo", sessions=1, git=True)
    second = env.work / "two" / "repo"
    shutil.copytree(first, second, symlinks=True)  # a clone: same root commit
    env.add_sessions(claude_project_path(str(second)), 1)
    env.sync()
    shutil.rmtree(first)
    shutil.rmtree(second)
    target = env.work / "three" / "repo"
    target.mkdir(parents=True)
    from tests.conftest import init_git  # a different repository entirely
    init_git(target)
    monkeypatch.setattr(auto, "_interactive", lambda: True)

    class NoInput:
        def readline(self):
            raise AssertionError("prompted without a unique candidate")

    monkeypatch.setattr(sys, "stdin", NoInput())
    snap = env.snapshot()
    run_auto(env, target, monkeypatch)
    assert env.snapshot() == snap


@needs_git
def test_weak_match_skipped_when_folder_already_has_sessions(env, monkeypatch):
    old = env.add_project("repo", sessions=1, git=True)
    env.sync()
    new = _copy_across_volumes(env, old, "elsewhere/repo")
    env.add_sessions(new, 1)
    monkeypatch.setattr(auto, "_interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    snap = env.snapshot()
    run_auto(env, new, monkeypatch)
    assert env.snapshot() == snap


def test_claude_config_dir_is_honoured(env, monkeypatch):
    # With CLAUDE_CONFIG_DIR, Claude Code keeps projects/, history.jsonl and
    # .claude.json all inside that directory.
    custom = env.root / "custom-config"
    shutil.move(str(env.claude_dir), str(custom))
    shutil.move(str(env.claude_json), str(custom / ".claude.json"))
    env.claude_dir, env.claude_json = custom, custom / ".claude.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    old = env.add_project("cfg", sessions=1)
    env.sync()
    new = renamed(env, old, "cfg2")
    monkeypatch.chdir(new)
    assert auto.run() == 0  # no explicit claude_dir: found via the environment
    assert env.project_dir(new).is_dir()
    assert new in env.read_config()["projects"]
    assert not (env.home / ".claude.json").exists()


# ─── Regressions from the adversarial review ───────────────────────────────

def _restore_cli(env, monkeypatch, answer="y", *args):
    from claudepath.cli import cmd_restore

    monkeypatch.setattr("claudepath.cli.find_claude_dir", lambda: env.claude_dir)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    cmd_restore(list(args))


def test_restore_never_reapplies_a_rolled_back_migration(env, monkeypatch, capsys):
    old = env.add_project("proj", sessions=1)
    env.sync()
    new = renamed(env, old, "proj2")

    def fail(*a, **k):
        raise OSError("late failure")

    monkeypatch.setattr("claudepath.migrate.update_usage_data", fail)
    assert run_hook(env, new, monkeypatch) == 1
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(env.home))
    # Life goes on: new prompts and settings appear...
    env.history.append({"display": "NEW", "pastedContents": {}, "timestamp": 9, "project": "/x"})
    env.config["projects"]["/new/project"] = {"hasTrustDialogAccepted": True}
    env.flush()
    snap = env.snapshot()
    # ...and `claudepath restore` must not replay the rolled-back journal.
    _restore_cli(env, monkeypatch)
    assert env.snapshot() == snap
    assert restore_journal(list_journals(get_journal_base(env.claude_dir))[0]["path"]) == []
    assert env.snapshot() == snap


def test_undo_keeps_what_claude_wrote_since_and_is_not_redone(env, monkeypatch, capsys):
    old = env.add_project("keep", sessions=2)
    env.sync()
    new = renamed(env, old, "keep2")
    run_auto(env, new, monkeypatch)
    session = next(env.project_dir(new).glob("*.jsonl"))
    # After the migration: Claude continues a session, a prompt is typed, a tool is approved.
    with open(session, "ab") as f:
        f.write(b'{"type":"user","cwd":"' + new.encode() + b'","message":"later"}\n')
    with open(env.claude_dir / "history.jsonl", "ab") as f:
        f.write(b'{"display":"typed later","pastedContents":{},"timestamp":5,"project":"' + new.encode() + b'"}\n')
    config = env.read_config()
    config["projects"][new]["allowedTools"].append("Bash(ls)")
    env.claude_json.write_text(json.dumps(config, indent=2))

    _restore_cli(env, monkeypatch)
    assert "Undone." in capsys.readouterr().out
    back = env.project_dir(old) / session.name
    assert not env.project_dir(new).exists() and back.exists()
    tail = json.loads(back.read_bytes().splitlines()[-1])
    assert tail == {"type": "user", "cwd": old, "message": "later"}  # kept, back under old
    history = [json.loads(l) for l in env.history_lines()]
    assert {h["project"] for h in history if h["display"].startswith("prompt")} == {old}
    assert [h["project"] for h in history if h["display"] == "typed later"] == [new]
    restored = env.read_config()["projects"]
    assert new not in restored and restored[old]["allowedTools"] == ["Bash(npm test)", "Bash(ls)"]
    # The undo sticks: the next launch does not migrate again.
    snap = env.snapshot()
    run_auto(env, new, monkeypatch)
    assert env.snapshot() == snap


def test_undo_leaves_sessions_started_since_at_the_new_location(env, monkeypatch):
    old = env.add_project("split", sessions=1)
    env.sync()
    new = renamed(env, old, "split2")
    run_auto(env, new, monkeypatch)
    migrated = next(env.project_dir(new).glob("*.jsonl")).stem
    # Since the move: a new session at the new path, and the migrated session
    # continued with a new subagent.
    (env.project_dir(new) / "later.jsonl").write_text('{"type":"user","cwd":"%s"}\n' % new)
    sub = env.project_dir(new) / migrated / "subagents" / "agent-new.jsonl"
    sub.write_text('{"type":"user","cwd":"%s"}\n' % new)
    _restore_cli(env, monkeypatch)
    assert sorted(p.name for p in env.project_dir(new).iterdir()) == ["later.jsonl"]
    back = env.project_dir(old) / migrated / "subagents" / "agent-new.jsonl"
    assert json.loads(back.read_text())["cwd"] == old  # went back with its session
    assert len(list(env.project_dir(old).glob("*.jsonl"))) == 1


def test_subfolder_whose_slug_equals_the_new_path_stays_separate(env, monkeypatch):
    old = env.add_project("app", sessions=2)
    web_ids = env.add_sessions(old + "/web", 1)
    (Path(old) / "web").mkdir()
    env.sync()
    new = renamed(env, old, "app-web")
    assert encode_path(new) == encode_path(old + "/web")  # the collision
    main_ids = {p.stem for p in env.project_dir(old).glob("*.jsonl")}
    run_auto(env, new, monkeypatch)
    assert {p.stem for p in env.project_dir(new).glob("*.jsonl")} == main_ids
    assert {p.stem for p in env.project_dir(new + "/web").glob("*.jsonl")} == set(web_ids)


def test_unconfirmed_inode_asks_instead_of_moving(env, monkeypatch):
    # Linux has no birth time; without git history an inode number alone
    # might be a recycled one belonging to a deleted folder.
    old = env.add_project("maybe", sessions=1)
    env.sync()
    reg_file = registry_path(env.claude_dir)
    data = json.loads(reg_file.read_text())
    data["projects"][old].pop("birth", None)
    reg_file.write_text(json.dumps(data))
    new = renamed(env, old, "maybe2")
    snap = env.snapshot()
    monkeypatch.setattr(auto, "_interactive", lambda: False)
    run_auto(env, new, monkeypatch)
    assert env.snapshot() == snap  # never moved silently

    monkeypatch.setattr(auto, "_interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    run_auto(env, new, monkeypatch)
    assert env.project_dir(new).is_dir() and not env.project_dir(old).exists()


def test_nfd_registry_key_does_not_wedge_auto(env, monkeypatch, capsys):
    import unicodedata

    path = env.add_project("프로젝트", sessions=1)
    reg = load_registry(env.claude_dir)
    from claudepath.registry import make_entry, save_registry

    reg["projects"][unicodedata.normalize("NFD", path)] = make_entry(path)
    save_registry(env.claude_dir, reg)
    for _ in range(2):
        assert run_auto(env, path, monkeypatch) == 0
    assert "skipped" not in capsys.readouterr().err
    assert list(load_registry(env.claude_dir)["projects"]) == [path]


def test_lone_surrogate_in_metadata_does_not_block_migration(env, monkeypatch):
    old = env.add_project("emoji", sessions=1)
    meta = env.claude_dir / "usage-data" / "session-meta"
    meta.mkdir(parents=True)
    (meta / "s.json").write_text('{"project_path": "%s", "first_prompt": "cut \\ud83d"}' % old)
    env.sync()
    new = renamed(env, old, "emoji2")
    run_auto(env, new, monkeypatch)
    assert env.project_dir(new).is_dir()
    text = (meta / "s.json").read_text()
    assert json.loads(text)["project_path"] == new and "\\ud83d" in text


def test_sigterm_mid_migration_rolls_back(env, monkeypatch):
    import signal

    old = env.add_project("term", sessions=2)
    env.sync()
    new = renamed(env, old, "term2")
    snap = env.snapshot()

    def killed(*a, **k):
        os.kill(os.getpid(), signal.SIGTERM)  # e.g. the terminal window closes

    monkeypatch.setattr("claudepath.migrate.update_claude_json", killed)
    monkeypatch.chdir(new)
    with pytest.raises(SystemExit) as exc:
        auto.run(claude_dir=env.claude_dir)
    assert exc.value.code == 128 + signal.SIGTERM
    assert env.snapshot() == snap
    assert list_journals(get_journal_base(env.claude_dir))[0]["status"] == "rolled back"


def test_interrupted_migration_is_recovered_on_next_launch(env, monkeypatch, capsys):
    old = env.add_project("crash", sessions=2)
    env.sync()
    new = renamed(env, old, "crash2")
    snap = env.snapshot()

    class PowerCut(BaseException):
        pass

    def fail(*a, **k):
        raise OSError("disk full")

    def die(*a, **k):
        raise PowerCut  # the process dies before it can roll back

    monkeypatch.setattr("claudepath.migrate.update_claude_json", fail)
    monkeypatch.setattr("claudepath.migrate._revert", die)
    with pytest.raises(PowerCut):
        run_hook(env, new, monkeypatch)  # leaves a half-done migration behind
    assert env.snapshot() != snap
    # Meanwhile, Claude Code keeps going elsewhere: a new prompt in another project.
    with open(env.claude_dir / "history.jsonl", "ab") as f:
        f.write(b'{"display":"elsewhere","pastedContents":{},"timestamp":7,"project":"/x"}\n')
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setattr(auto, "_claude_processes_in", lambda *paths: [])
    run_auto(env, new, monkeypatch)
    err = capsys.readouterr().err
    assert "rolled back an interrupted migration" in err and "sessions followed" in err
    assert env.project_dir(new).is_dir() and not env.project_dir(old).exists()
    assert b'"elsewhere"' in (env.claude_dir / "history.jsonl").read_bytes()  # not wiped
    statuses = [j["status"] for j in list_journals(get_journal_base(env.claude_dir))]
    assert statuses == ["complete", "rolled back (interrupted)"]


def test_memory_only_dir_with_an_ambiguous_slug_is_left_alone(env, monkeypatch):
    old = env.add_project("proj", sessions=1)
    (Path(old) / "a").mkdir()
    env.config["projects"][old + "/a"] = {}
    env.config["projects"][old + "-a"] = {}  # same slug, different folder
    env.flush()
    ambiguous = env.project_dir(old + "/a")
    (ambiguous / "memory").mkdir(parents=True)
    (ambiguous / "memory" / "MEMORY.md").write_text("whose?")
    env.sync()
    new = renamed(env, old, "proj2")
    run_auto(env, new, monkeypatch)
    assert ambiguous.is_dir() and (ambiguous / "memory" / "MEMORY.md").read_text() == "whose?"


def test_merging_into_another_folders_directory_is_refused(env, monkeypatch, capsys):
    old = env.add_project("src", sessions=1)
    env.sync()
    other = env.add_project("my_app", sessions=1)  # its slug: "...-my-app"
    new = renamed(env, old, "my-app")
    assert encode_path(new) == encode_path(other)
    snap = env.snapshot()
    assert run_hook(env, new, monkeypatch) == 1
    assert env.snapshot() == snap
    assert "a different folder with the same Claude Code directory name" in capsys.readouterr().err


def test_running_session_detection_sees_ancestors_and_utf8_names(monkeypatch):
    import subprocess

    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("env", {}).get("LC_ALL")))

        class R:
            pass

        r = R()
        if cmd[0] == "pgrep":
            r.stdout = "41\n42\n"
        else:
            r.stdout = (b"p41\nfcwd\nn/Users/me/\\xed\\x94\\x84\\xeb\\xa1\\x9c/sub\n"
                        b"p42\nfcwd\nn/Users/me/elsewhere\n")
        return r

    real_isdir = os.path.isdir
    monkeypatch.setattr(auto.os.path, "isdir", lambda p: False if p == "/proc/self" else real_isdir(p))
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(auto.sys, "platform", "darwin")
    assert auto._claude_processes_in("/Users/me/프로", "/old/path") == [41]
    assert calls[0][0] == ["pgrep", "-a", "-x", "claude"]
    assert calls[1][1] == "en_US.UTF-8"


# ─── Regressions from the second review ───────────────────────────────────

def _completed_migration(env, monkeypatch, name="u"):
    old = env.add_project(name, sessions=2)
    env.sync()
    new = renamed(env, old, name + "-moved")
    run_auto(env, new, monkeypatch)
    return old, new, list_journals(get_journal_base(env.claude_dir))[0]["path"]


def test_undo_refuses_when_the_old_path_was_used_again(env, monkeypatch, capsys):
    from claudepath.migrate import MigrationError, undo_migration

    old, new, backup = _completed_migration(env, monkeypatch)
    # The swap case: a new folder at the old path, used with Claude Code.
    os.makedirs(old)
    (env.project_dir(old)).mkdir()
    (env.project_dir(old) / "fresh.jsonl").write_text('{"type":"user","cwd":"%s"}\n' % old)
    config = env.read_config()
    config["projects"][old] = {"mcpServers": {"x": {"env": {"TOKEN": "t"}}}}
    env.claude_json.write_text(json.dumps(config, indent=2))
    snap = env.snapshot()
    with pytest.raises(MigrationError) as exc:
        undo_migration(backup, env.claude_dir)
    message = str(exc.value)
    assert "now holds another folder" in message and "used at" in message
    assert "settings for" in message and "Nothing was changed" in message
    assert env.snapshot() == snap
    assert list_journals(get_journal_base(env.claude_dir))[0]["status"] == "complete"  # retry later
    with pytest.raises(SystemExit):
        _restore_cli(env, monkeypatch)  # the CLI refuses the same way
    assert "Nothing was changed" in capsys.readouterr().err
    assert env.snapshot() == snap


def test_undo_refuses_while_a_session_runs(env, monkeypatch):
    from claudepath.migrate import MigrationError, undo_migration

    old, new, backup = _completed_migration(env, monkeypatch)
    monkeypatch.setattr(auto, "_claude_processes_in", lambda *paths: [77])
    with pytest.raises(MigrationError, match="pid 77"):
        undo_migration(backup, env.claude_dir)


def test_restoring_twice_does_not_redo_the_migration(env, monkeypatch, capsys):
    old, new, backup = _completed_migration(env, monkeypatch)
    _restore_cli(env, monkeypatch)
    assert env.project_dir(old).is_dir()
    capsys.readouterr()
    snap = env.snapshot()
    _restore_cli(env, monkeypatch)
    assert env.snapshot() == snap
    assert "Nothing to restore" in capsys.readouterr().out


def test_recovery_keeps_settings_written_after_the_crash(env, monkeypatch):
    old = env.add_project("cfg-crash", sessions=1)
    env.sync()
    new = renamed(env, old, "cfg-crash2")

    class PowerCut(BaseException):
        pass

    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("claudepath.migrate.update_usage_data", fail)  # after the config step
    monkeypatch.setattr("claudepath.migrate._revert", lambda *a, **k: (_ for _ in ()).throw(PowerCut()))
    with pytest.raises(PowerCut):
        run_hook(env, new, monkeypatch)
    config = env.read_config()
    assert new in config["projects"]  # the half-done migration got this far
    config["projects"]["/added/later"] = {"hasTrustDialogAccepted": True}
    env.claude_json.write_text(json.dumps(config, indent=2))
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setattr(auto, "_claude_processes_in", lambda *paths: [])
    other = env.work / "elsewhere"
    other.mkdir()
    run_auto(env, other, monkeypatch)  # any launch recovers
    config = env.read_config()
    assert old in config["projects"] and new not in config["projects"]
    assert "/added/later" in config["projects"]  # not wiped by recovery


@needs_git
def test_git_confirms_identity_only_at_the_same_place_in_the_repo(env, monkeypatch):
    # Without a birth time (Linux), the git root commit must match *and* the
    # folder must sit at the same path inside the repository.
    repo = env.work / "mono"
    (repo / "src").mkdir(parents=True)
    from tests.conftest import init_git

    init_git(repo)
    src = claude_project_path(str(repo / "src"))
    env.add_sessions(src, 1)
    env.sync()
    reg_file = registry_path(env.claude_dir)
    data = json.loads(reg_file.read_text())
    data["projects"][src].pop("birth", None)
    reg_file.write_text(json.dumps(data))
    assert data["projects"][src]["git_prefix"] == "src"

    os.rename(repo / "src", repo / "lib")  # another place in the same repository
    lib = claude_project_path(str(repo / "lib"))
    monkeypatch.setattr(auto, "_interactive", lambda: False)
    snap = env.snapshot()
    run_auto(env, lib, monkeypatch)
    assert env.snapshot() == snap  # unconfirmed: not moved silently

    os.rename(repo / "lib", repo / "src")
    os.rename(repo, env.work / "mono-renamed")  # whole repo moved: same place inside it
    moved = claude_project_path(str(env.work / "mono-renamed" / "src"))
    run_auto(env, moved, monkeypatch)
    assert env.project_dir(moved).is_dir() and not env.project_dir(src).exists()


def test_case_only_undo_with_newer_sessions_is_refused(env, monkeypatch):
    from claudepath.migrate import MigrationError, undo_migration

    old = env.add_project("CaseUndo", sessions=1)
    env.sync()
    if not os.path.exists(old.replace("CaseUndo", "caseundo")):
        pytest.skip("case-sensitive filesystem")
    os.rename(old, env.work / "tmp-case")
    os.rename(env.work / "tmp-case", env.work / "caseundo")
    new = claude_project_path(str(env.work / "caseundo"))
    run_auto(env, new, monkeypatch)
    (env.project_dir(new) / "later.jsonl").write_text('{"type":"user","cwd":"%s"}\n' % new)
    backup = list_journals(get_journal_base(env.claude_dir))[0]["path"]
    with pytest.raises(MigrationError, match="only the letter case changed"):
        undo_migration(backup, env.claude_dir)
