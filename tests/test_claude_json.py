"""
Tests for ~/.claude.json and history.jsonl updates, and the file helpers they
rely on (proper-lockfile compatible locks, atomic writes).
"""

import json
import os
import threading
import time

import pytest

from claudepath.fsutil import AtomicFile, LockTimeout, atomic_write, file_lock, process_lock
from claudepath.updaters import rewrite_history_projects, update_claude_json
from tests.conftest import compact


def write_config(path, data, mode=0o600):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(path, mode)


def test_rekeys_projects_in_place_with_subfolders(tmp_path):
    cfg = tmp_path / ".claude.json"
    write_config(cfg, {"projects": {
        "/a": {"x": 1}, "/old": {"allowedTools": ["Bash"]}, "/old/sub": {"t": True},
        "/older": {"keep": 1}, "/z": {},
    }, "numStartups": 5})
    moved, repos = update_claude_json(cfg, "/old", "/new")
    data = json.loads(cfg.read_text())
    assert list(data["projects"]) == ["/a", "/new", "/new/sub", "/older", "/z"]  # order kept
    assert data["projects"]["/new"] == {"allowedTools": ["Bash"]}
    assert data["numStartups"] == 5
    assert moved == [("/old", "/new"), ("/old/sub", "/new/sub")] and repos == 0
    assert cfg.read_text().endswith("}\n")
    assert oct(cfg.stat().st_mode & 0o777) == oct(0o600)


def test_merge_old_values_win_and_new_only_keys_survive(tmp_path):
    cfg = tmp_path / ".claude.json"
    write_config(cfg, {"projects": {
        "/new": {"allowedTools": [], "hasTrustDialogAccepted": False, "onlyNew": 1},
        "/old": {"allowedTools": ["Bash(make)"], "hasTrustDialogAccepted": True},
    }})
    update_claude_json(cfg, "/old", "/new")
    assert json.loads(cfg.read_text())["projects"] == {
        "/new": {"allowedTools": ["Bash(make)"], "hasTrustDialogAccepted": True, "onlyNew": 1}
    }


def test_github_repo_paths_follow_and_dedupe(tmp_path):
    cfg = tmp_path / ".claude.json"
    write_config(cfg, {"projects": {}, "githubRepoPaths": {
        "me/a": ["/old", "/new", "/x"], "me/b": ["/elsewhere"]}})
    moved, repos = update_claude_json(cfg, "/old", "/new")
    data = json.loads(cfg.read_text())
    assert data["githubRepoPaths"] == {"me/a": ["/new", "/x"], "me/b": ["/elsewhere"]}
    assert repos == 1 and moved == []


def test_nothing_to_change_leaves_file_untouched(tmp_path):
    cfg = tmp_path / ".claude.json"
    write_config(cfg, {"projects": {"/a": {}}})
    ino, mtime = cfg.stat().st_ino, cfg.stat().st_mtime_ns
    assert update_claude_json(cfg, "/old", "/new") == ([], 0)
    assert (cfg.stat().st_ino, cfg.stat().st_mtime_ns) == (ino, mtime)


def test_dry_run_and_symlinked_config(tmp_path):
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    write_config(real, {"projects": {"/old": {}}})
    link = tmp_path / ".claude.json"
    link.symlink_to(real)
    assert update_claude_json(link, "/old", "/new", dry_run=True)[0] == [("/old", "/new")]
    assert "/old" in json.loads(real.read_text())["projects"]
    update_claude_json(link, "/old", "/new")
    assert link.is_symlink() and "/new" in json.loads(real.read_text())["projects"]


def test_waits_for_claude_codes_lock(tmp_path):
    cfg = tmp_path / ".claude.json"
    write_config(cfg, {"projects": {"/old": {}}})
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()  # a running Claude Code instance is saving its config

    def release():
        time.sleep(0.3)
        lock.rmdir()

    t = threading.Thread(target=release)
    t.start()
    start = time.monotonic()
    update_claude_json(cfg, "/old", "/new")
    t.join()
    assert time.monotonic() - start >= 0.25
    assert "/new" in json.loads(cfg.read_text())["projects"]
    assert not lock.exists()


def test_history_rewrite_honours_lock_and_keeps_other_lines(tmp_path):
    hist = tmp_path / "history.jsonl"
    lines = [compact({"display": "a", "pastedContents": {}, "timestamp": 1, "project": "/old"}),
             compact({"display": "b", "pastedContents": {}, "timestamp": 2, "project": "/other"}),
             compact({"display": "c /old", "pastedContents": {}, "timestamp": 3, "project": "/old/x"})]
    hist.write_text("\n".join(lines) + "\n")
    os.utime(hist, ns=(1, 2))
    (tmp_path / "history.jsonl.lock").mkdir()
    os.utime(tmp_path / "history.jsonl.lock", (time.time() - 60, time.time() - 60))  # stale
    assert rewrite_history_projects(hist, "/old", "/new") == 2
    out = hist.read_text().splitlines()
    assert out[1] == lines[1]
    assert json.loads(out[0])["project"] == "/new"
    assert json.loads(out[2]) == {"display": "c /old", "pastedContents": {}, "timestamp": 3,
                                  "project": "/new/x"}
    assert hist.stat().st_mtime_ns == 2
    assert not (tmp_path / "history.jsonl.lock").exists()


# ─── fsutil ────────────────────────────────────────────────────────────────

def test_file_lock_times_out_on_a_fresh_lock(tmp_path):
    (tmp_path / "f.lock").mkdir()
    with pytest.raises(LockTimeout):
        with file_lock(tmp_path / "f", timeout=0.2):
            pass


def _hold_lock_in_subprocess(lock):
    import subprocess
    import sys

    code = ("import sys, time; from claudepath.fsutil import process_lock\n"
            "with process_lock(sys.argv[1]):\n    print('held', flush=True); time.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE)
    assert proc.stdout.readline().strip() == b"held"
    return proc


def test_process_lock_waits_for_a_live_holder_and_is_freed_when_it_dies(tmp_path):
    lock = tmp_path / "registry.lock"
    holder = _hold_lock_in_subprocess(lock)
    try:
        with pytest.raises(LockTimeout):
            with process_lock(lock, timeout=0.2):
                pass
    finally:
        holder.kill()  # no cleanup runs: the kernel releases the lock
        holder.wait()
    with process_lock(lock, timeout=1):
        assert lock.read_text() == str(os.getpid())


def test_atomic_file_keeps_mode_and_times_and_cleans_up(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"old")
    os.chmod(f, 0o604)
    os.utime(f, ns=(10, 20))
    atomic_write(f, b"new", keep_times=True)
    assert f.read_bytes() == b"new" and oct(f.stat().st_mode & 0o777) == oct(0o604)
    assert f.stat().st_mtime_ns == 20

    with pytest.raises(RuntimeError):
        with AtomicFile(f) as out:
            out.write(b"partial")
            raise RuntimeError
    assert f.read_bytes() == b"new"

    atomic = AtomicFile(f)
    with atomic as out:
        out.write(b"discarded")
        atomic.discard()
    assert f.read_bytes() == b"new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]
