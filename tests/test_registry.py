"""
Tests for the identity registry: identity capture, git details, sync, rekey.
"""

import json
import os
import subprocess
import unicodedata

import pytest

from claudepath import registry as R
from tests.conftest import init_git, needs_git


def test_claude_project_path_is_nfc_realpath(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert R.claude_project_path(str(link)) == os.path.realpath(real)
    nfd = unicodedata.normalize("NFD", "/tmp/한글")
    assert R.claude_project_path(nfd) == unicodedata.normalize("NFC", os.path.realpath(nfd))
    assert R.claude_project_path("/") == "/"


def test_canonical_project_path_reports_on_disk_case(tmp_path):
    d = tmp_path / "MixedCase"
    d.mkdir()
    lower = str(tmp_path / "mixedcase")
    if not os.path.exists(lower):
        pytest.skip("case-sensitive filesystem")
    assert R.canonical_project_path(lower) == R.claude_project_path(str(d))
    assert os.getcwd() != str(d)  # our own working directory is restored


def test_identity_and_birth_guard(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    ident = R.dir_identity(str(d))
    st = os.stat(d)
    assert ident["dev"] == st.st_dev and ident["ino"] == st.st_ino
    assert R.same_identity(ident, st)
    if "birth" in ident:
        assert not R.same_identity(dict(ident, birth=ident["birth"] + 10), st)
    assert R.dir_identity(str(tmp_path / "missing")) is None
    (tmp_path / "file").write_text("x")
    assert R.dir_identity(str(tmp_path / "file")) is None


def test_git_identity_outside_a_repo_runs_no_git(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git was run")

    monkeypatch.setattr(subprocess, "run", boom)
    assert R.git_identity(str(tmp_path)) == {"git_root": None}


@needs_git
def test_git_identity_root_prefix_and_remote(env, tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg" / "a").mkdir(parents=True)
    init_git(repo, remote="https://user:s3cret@github.com/me/repo.git")
    root = subprocess.run(["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    top = R.git_identity(str(repo))
    assert top == {"git_root": root, "git_prefix": "",
                   "git_remote": "https://github.com/me/repo.git"}  # credentials stripped
    assert R.git_identity(str(repo / "pkg" / "a"))["git_prefix"] == "pkg/a"


def test_load_save_roundtrip_and_unreadable_file(tmp_path):
    claude_dir = tmp_path / ".claude"
    assert R.load_registry(claude_dir) == R.empty_registry()
    reg = R.empty_registry()
    reg["projects"]["/b"] = {"dev": 1, "ino": 2}
    reg["projects"]["/a"] = {"dev": 1, "ino": 3}
    R.save_registry(claude_dir, reg)
    data = json.loads(R.registry_path(claude_dir).read_text())
    assert list(data["projects"]) == ["/a", "/b"]  # sorted for readable diffs

    R.registry_path(claude_dir).write_text("{not json")
    broken = R.load_registry(claude_dir)
    assert broken["projects"] == {} and broken["_unreadable"]
    R.save_registry(claude_dir, broken)
    assert list(R.registry_dir(claude_dir).glob("registry.json.unreadable-*"))


def test_sync_adds_refreshes_and_keeps_changed_identity(tmp_path):
    a, b, gone = tmp_path / "a", tmp_path / "b", tmp_path / "gone"
    a.mkdir()
    b.mkdir()
    reg = R.empty_registry()
    reg["projects"][str(b)] = {"dev": 1, "ino": 99999999}  # recorded inode != current
    result = R.sync_registry(reg, [str(a), str(b), str(gone)], with_git=False)
    assert result.added == [str(a)]
    assert result.changed == [str(b)] and reg["projects"][str(b)]["ino"] == 99999999
    assert result.missing == [str(gone)]


def test_rekey_moves_entry_and_subfolders(tmp_path):
    new = tmp_path / "new"
    (new / "sub").mkdir(parents=True)
    reg = R.empty_registry()
    reg["projects"]["/old"] = {"dev": 0, "ino": 0, "git_root": "abc"}
    reg["projects"]["/old/sub"] = {"dev": 0, "ino": 0}
    reg["projects"]["/older"] = {"dev": 0, "ino": 1}
    reg["declined"] = [{"from": "/old", "to": "/x"}, {"from": "/old", "to": str(new)}]
    assert R.rekey(reg, "/old", str(new)) == 2
    assert set(reg["projects"]) == {str(new), str(new / "sub"), "/older"}
    assert reg["projects"][str(new)]["git_root"] == "abc"
    assert reg["projects"][str(new)]["ino"] == os.stat(new).st_ino
    assert reg["declined"] == [{"from": "/old", "to": "/x"}]  # only the executed pair is dropped


def test_known_project_paths_reads_sessions_and_config(env):
    a = env.add_project("a", sessions=1)
    env.config["projects"]["/only/in/config"] = {}
    env.flush()
    paths = R.known_project_paths(env.claude_dir, env.claude_json)
    assert a in paths and "/only/in/config" in paths and "/somewhere/else" in paths
