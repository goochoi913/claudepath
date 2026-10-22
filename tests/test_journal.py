"""
Tests for the automatic-mode backup journal and its restore, including the
`claudepath restore` command.
"""

import json
import os
import sys

from claudepath.backup import Journal, list_journals, restore_journal
from claudepath.cli import cmd_restore


def test_save_copies_by_default_and_links_on_request(tmp_path):
    j = Journal(tmp_path / "backups", "test")
    copied, linked = tmp_path / "c.txt", tmp_path / "l.jsonl"
    copied.write_text("c")
    linked.write_text("l")
    j.save(copied)
    j.save_linked(linked)
    j.save(copied)  # once per path
    ops = j.manifest["ops"]
    assert [o["op"] for o in ops] == ["save", "save"]
    assert os.stat(j.dir / ops[0]["backup"]).st_ino != copied.stat().st_ino
    assert os.stat(j.dir / ops[1]["backup"]).st_ino == linked.stat().st_ino
    # A copy survives an in-place rewrite of the original.
    copied.write_text("changed in place")
    assert (j.dir / ops[0]["backup"]).read_text() == "c"


def test_restore_undoes_moves_saves_deletes_and_mkdirs_in_reverse(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "a.jsonl").write_text("original")
    (src / "dup.txt").write_text("dup")
    j = Journal(tmp_path / "backups", "test")
    j.moved(src, dst)
    os.rename(src, dst)
    j.save_linked(dst / "a.jsonl")
    tmp = dst / "a.tmp"
    tmp.write_text("rewritten")
    os.replace(tmp, dst / "a.jsonl")
    j.deleted(dst / "dup.txt")
    (dst / "dup.txt").unlink()
    (dst / "made").mkdir()
    j.made_dir(dst / "made")
    j.finish()

    assert restore_journal(j.dir) == []
    assert not dst.exists()
    assert (src / "a.jsonl").read_text() == "original"
    assert (src / "dup.txt").read_text() == "dup"
    assert json.loads((j.dir / "manifest.json").read_text())["status"] == "restored"
    assert restore_journal(j.dir) == []  # idempotent


def test_restore_reports_a_conflicting_path(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    dst.mkdir()
    src.mkdir()  # recreated after the move: can't move dst back over it
    j = Journal(tmp_path / "backups", "test")
    j.moved(src, dst)
    problems = restore_journal(j.dir)
    assert problems and "exists" in problems[0]


def test_cli_restore_lists_and_restores_journal(tmp_path, monkeypatch, capsys):
    claude_dir = tmp_path / ".claude"
    f = tmp_path / "file.txt"
    f.write_text("before")
    j = Journal(claude_dir / "claudepath" / "backups", "install", {"rc": str(f)})
    j.save(f)
    f.write_text("after")
    j.finish()
    monkeypatch.setattr("claudepath.cli.find_claude_dir", lambda: claude_dir)
    monkeypatch.setenv("NO_COLOR", "1")

    cmd_restore(["--list"])
    out = capsys.readouterr().out
    assert j.dir.name in out and "install" in out

    monkeypatch.setattr("builtins.input", lambda _: "y")
    cmd_restore([])  # newest backup is the journal
    assert f.read_text() == "before"
    assert "Restored successfully" in capsys.readouterr().out
    assert list_journals(claude_dir / "claudepath" / "backups")[0]["status"] == "restored"


def test_cli_restore_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    claude_dir = tmp_path / ".claude"
    f = tmp_path / "file.txt"
    f.write_text("before")
    j = Journal(claude_dir / "claudepath" / "backups", "auto: folder moved")
    j.save(f)
    f.write_text("after")
    j.finish()
    monkeypatch.setattr("claudepath.cli.find_claude_dir", lambda: claude_dir)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("asked")))
    cmd_restore(["--dry-run"])
    assert f.read_text() == "after"
    assert "would restore" in capsys.readouterr().out
