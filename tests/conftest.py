"""
Shared fixtures for the automatic-mode tests: a fake HOME holding a Claude
Code data directory (sessions, history.jsonl, ~/.claude.json) and real project
folders, so moves and renames can be exercised end to end.
"""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from claudepath.encoder import encode_path
from claudepath.registry import (
    claude_project_path,
    known_project_paths,
    load_registry,
    save_registry,
    sync_registry,
)

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git not installed")


def compact(obj) -> str:
    """JSON the way Claude Code (JSON.stringify) writes it."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def session_lines(project: str, session_id: str) -> List[bytes]:
    """Realistic session lines: large values before the top-level cwd, nested
    "cwd" keys and JSON-in-strings that must stay untouched, a cd into a
    subfolder, a sibling path that merely shares the prefix, lines without cwd.
    """
    sub = project + "/sub"
    sibling = project + "-other"
    lines = [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": session_id},
        {"parentUuid": None, "isSidechain": False, "promptId": "p1", "type": "user",
         "message": {"role": "user", "content": f"look at {project}/src/app.py"},
         "uuid": "u1", "timestamp": "2026-10-01T10:00:00.000Z", "userType": "external",
         "entrypoint": "cli", "cwd": project, "sessionId": session_id,
         "version": "2.1.308", "gitBranch": "main"},
        {"parentUuid": "u1", "isSidechain": False, "type": "assistant",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash",
              "input": {"command": "ls", "cwd": project, "file_path": f"{project}/src/app.py"}}]},
         "uuid": "a1", "userType": "external", "entrypoint": "cli",
         "cwd": project, "sessionId": session_id},
        {"parentUuid": "a1", "isSidechain": False,
         "attachment": {"type": "hook_success", "stdout": compact({"cwd": project}),
                        "data": {"cwd": project}},
         "type": "attachment", "uuid": "t1", "cwd": sub, "sessionId": session_id},
        {"parentUuid": "t1", "type": "user",
         "message": {"role": "user", "content": f'quoted "cwd":"{project}" text'},
         "uuid": "u2", "cwd": sibling, "sessionId": session_id},
        {"type": "last-prompt", "lastPrompt": f"cd {project}", "sessionId": session_id},
    ]
    return [(compact(line) + "\n").encode("utf-8") for line in lines]


def init_git(folder: Path, remote: str = "") -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)

    git("init", "-q")
    # A unique message: two empty commits made in the same second with the
    # same author would otherwise be the very same root commit.
    git("-c", "user.name=t", "-c", "user.email=t@example.com",
        "commit", "-q", "--allow-empty", "-m", f"init {uuid.uuid4()}")
    if remote:
        git("remote", "add", "origin", remote)


class FakeEnv:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.claude_dir = self.home / ".claude"
        self.claude_json = self.home / ".claude.json"
        self.work = self.home / "work"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.work.mkdir()
        self.history: List[Dict] = []
        self.config: Dict = {
            "numStartups": 3,
            "projects": {"/somewhere/else": {"allowedTools": [], "hasTrustDialogAccepted": False}},
            "githubRepoPaths": {"me/other": ["/somewhere/else"]},
            "oauthAccount": {"emailAddress": "someone@example.com"},
        }
        self.flush()

    # Claude Code data ------------------------------------------------------

    def project_dir(self, path: str) -> Path:
        return self.claude_dir / "projects" / encode_path(path)

    def add_project(self, rel: str, sessions: int = 2, git: bool = False, remote: str = "",
                    memory: str = "# notes\n", subagent: bool = True) -> str:
        folder = self.work / rel
        (folder / "sub").mkdir(parents=True, exist_ok=True)
        (folder / "README.md").write_text("hello\n")
        if git:
            init_git(folder, remote)
        path = claude_project_path(str(folder))
        self.add_sessions(path, sessions, memory=memory, subagent=subagent)
        if remote:
            self.config["githubRepoPaths"].setdefault(remote.split(":")[-1], []).append(path)
            self.flush()
        return path

    def add_sessions(self, path: str, count: int = 1, memory: str = "", subagent: bool = False,
                     settings: Dict = None) -> List[str]:
        d = self.project_dir(path)
        d.mkdir(parents=True, exist_ok=True)
        ids = []
        for i in range(count):
            sid = str(uuid.uuid4())
            ids.append(sid)
            f = d / f"{sid}.jsonl"
            f.write_bytes(b"".join(session_lines(path, sid)))
            os.chmod(f, 0o600)
            os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
            if subagent:
                agents = d / sid / "subagents"
                agents.mkdir(parents=True)
                (agents / "agent-a1.jsonl").write_bytes(
                    (compact({"type": "user", "isSidechain": True, "cwd": path,
                              "sessionId": sid}) + "\n").encode()
                )
                (d / sid / "tool-results").mkdir()
                (d / sid / "tool-results" / "r1.txt").write_text(f"output in {path}\n")
            self.history.append({"display": f"prompt {i}", "pastedContents": {},
                                 "timestamp": 1_700_000_000_000 + i, "project": path,
                                 "sessionId": sid})
        if memory:
            (d / "memory").mkdir(exist_ok=True)
            (d / "memory" / "MEMORY.md").write_text(memory)
        self.config["projects"][path] = settings or {
            "allowedTools": ["Bash(npm test)"], "hasTrustDialogAccepted": True,
            "mcpServers": {"db": {"command": "db-mcp"}}, "lastSessionId": ids[-1] if ids else None,
        }
        self.flush()
        return ids

    def flush(self) -> None:
        hist = self.claude_dir / "history.jsonl"
        hist.write_bytes(b"".join((compact(h) + "\n").encode() for h in self.history))
        self.claude_json.write_text(json.dumps(self.config, indent=2, ensure_ascii=False))
        os.chmod(self.claude_json, 0o600)

    def sync(self) -> None:
        reg = load_registry(self.claude_dir)
        sync_registry(reg, known_project_paths(self.claude_dir, self.claude_json))
        save_registry(self.claude_dir, reg)

    def read_config(self) -> Dict:
        return json.loads(self.claude_json.read_text())

    def history_lines(self) -> List[bytes]:
        return (self.claude_dir / "history.jsonl").read_bytes().splitlines(keepends=True)

    def snapshot(self) -> Dict[str, Tuple[bytes, int]]:
        """Bytes and mtime of every Claude Code data file (not claudepath's own)."""
        snap = {}
        for f in sorted(self.claude_dir.rglob("*")):
            if f.is_file() and "claudepath" not in f.relative_to(self.claude_dir).parts:
                snap[str(f.relative_to(self.claude_dir))] = (f.read_bytes(), f.stat().st_mtime_ns)
        snap[".claude.json"] = (self.claude_json.read_bytes(), self.claude_json.stat().st_mtime_ns)
        return snap


@pytest.fixture
def env(tmp_path, monkeypatch) -> FakeEnv:
    root = Path(os.path.realpath(tmp_path))
    fake = FakeEnv(root)
    monkeypatch.setenv("HOME", str(fake.home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (root / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(root / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Hermetic: never look at the real machine's running Claude processes.
    monkeypatch.setattr("claudepath.auto._claude_processes_in", lambda *paths: [])
    return fake
