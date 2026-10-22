"""
Project identity registry for the automatic mode.

~/.claude/claudepath/registry.json remembers, for every project path Claude
Code has been used in, enough about the folder to recognise it after it has
been moved or renamed:

    dev, ino     device and inode of the directory -- unchanged by a rename or
                 a move within the same volume
    birth        the directory's creation time, where the OS reports one
                 (macOS); guards against a recycled inode number
    git_root     root commit of the repository the folder is in -- survives a
                 copy to another volume, which gets a new inode
    git_prefix   the folder's path inside that repository
    git_remote   URL of the "origin" remote (credentials stripped)

Paths are stored the way Claude Code keys projects: the NFC-normalised
realpath of the working directory (Claude Code 2.1.x computes
`realpathSync(process.cwd()).normalize("NFC")`).
"""

import json
import os
import re
import stat
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from claudepath.encoder import encode_path
from claudepath.fsutil import atomic_write, process_lock

REGISTRY_VERSION = 1
_GIT_TIMEOUT = 3.0


# ─── Locations ─────────────────────────────────────────────────────────────

def registry_dir(claude_dir: Path) -> Path:
    """claudepath's own data directory: ~/.claude/claudepath/."""
    return claude_dir / "claudepath"


def registry_path(claude_dir: Path) -> Path:
    return registry_dir(claude_dir) / "registry.json"


def registry_lock(claude_dir: Path, timeout: float = 3.0):
    """Lock held by any claudepath command that writes the registry or migrates."""
    registry_dir(claude_dir).mkdir(parents=True, exist_ok=True)
    return process_lock(registry_dir(claude_dir) / "registry.lock", timeout=timeout)


# ─── Paths as Claude Code sees them ────────────────────────────────────────

def claude_project_path(path: str) -> str:
    """The project path Claude Code would use for `path`: realpath + NFC."""
    if path != "/":
        path = os.path.realpath(path)
    return unicodedata.normalize("NFC", path)


def canonical_project_path(path: str) -> Optional[str]:
    """What Claude Code would record if launched in `path` right now.

    Changes into the directory and asks the kernel for the working directory,
    exactly as Claude Code does, which also yields the on-disk letter case on
    case-insensitive volumes (so a case-only rename is detectable). Returns
    None if the directory cannot be entered.
    """
    try:
        here = os.open(".", os.O_RDONLY)
    except OSError:
        return None
    try:
        os.chdir(path)
        return claude_project_path(os.getcwd())
    except OSError:
        return None
    finally:
        try:
            os.fchdir(here)
        finally:
            os.close(here)


def project_dir_for(claude_dir: Path, project_path: str) -> Path:
    return claude_dir / "projects" / encode_path(project_path)


def has_sessions(claude_dir: Path, project_path: str) -> bool:
    """True if Claude Code has at least one session file for `project_path`."""
    try:
        with os.scandir(project_dir_for(claude_dir, project_path)) as it:
            return any(e.name.endswith(".jsonl") for e in it)
    except OSError:
        return False


# ─── Identity ──────────────────────────────────────────────────────────────

def dir_identity(path: str) -> Optional[Dict]:
    """Device, inode and (where available) birth time of a directory."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return identity_from_stat(st)


def identity_from_stat(st: os.stat_result) -> Dict:
    ident: Dict = {"dev": st.st_dev, "ino": st.st_ino}
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        ident["birth"] = birth
    return ident


def same_identity(entry: Dict, st: os.stat_result) -> bool:
    """Does a registry entry describe the directory `st` was taken from?"""
    if entry.get("dev") != st.st_dev or entry.get("ino") != st.st_ino:
        return False
    return birth_matches(entry, st)


def identity_match(entry: Dict, st: os.stat_result, git: Optional[Dict] = None) -> str:
    """How sure is it that `st` is the folder the entry was recorded for?

    "same"         same device+inode, confirmed by the birth time (macOS) or,
                   where there is none, by the same git root commit *and* the
                   same path inside that repository
    "different"    the inode differs, or the confirming evidence contradicts it
    "unconfirmed"  same device+inode but nothing to confirm it: on Linux,
                   inode numbers of deleted folders are quickly reused (another
                   clone of the same repository can get one), so an inode alone
                   is not proof

    `git` is the folder's current git_identity(), when the caller has it.
    """
    if entry.get("dev") != st.st_dev or entry.get("ino") != st.st_ino:
        return "different"
    recorded, current = entry.get("birth"), getattr(st, "st_birthtime", None)
    if recorded is not None and current is not None:
        return "same" if abs(float(recorded) - float(current)) < 1e-3 else "different"
    if entry.get("git_root") and git and git.get("git_root"):
        if entry["git_root"] != git["git_root"]:
            return "different"
        if (entry.get("git_prefix") or "") != (git.get("git_prefix") or ""):
            return "unconfirmed"  # same repository, another place in it
        return "same"
    return "unconfirmed"


def birth_matches(entry: Dict, st: os.stat_result) -> bool:
    """False only when both birth times are known and differ (recycled inode)."""
    recorded = entry.get("birth")
    current = getattr(st, "st_birthtime", None)
    if recorded is None or current is None:
        return True
    return abs(float(recorded) - float(current)) < 1e-3


def inside_git_worktree(path: str) -> bool:
    """Cheap check (no subprocess) for a .git entry in `path` or a parent.

    Avoids running git -- and, on a Mac without the command line tools, the
    "install developer tools" dialog -- for folders that aren't repositories.
    """
    p = Path(path)
    for candidate in (p, *p.parents):
        if os.path.lexists(candidate / ".git"):
            return True
    return False


def _git(path: str, *args: str) -> Optional[str]:
    import subprocess

    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        proc = subprocess.run(
            ["git", "-C", path, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _strip_credentials(url: str) -> str:
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@", r"\1", url)


def git_identity(path: str) -> Dict:
    """Root commit, path inside the repo and origin URL; git_root None if not a repo."""
    if not inside_git_worktree(path):
        return {"git_root": None}
    roots = _git(path, "rev-list", "--max-parents=0", "HEAD")
    if not roots:
        return {"git_root": None}
    prefix = _git(path, "rev-parse", "--show-prefix") or ""
    remote = _git(path, "config", "--get", "remote.origin.url")
    return {
        "git_root": min(roots.split()),
        "git_prefix": prefix.rstrip("/"),
        "git_remote": _strip_credentials(remote) if remote else None,
    }


def make_entry(path: str, with_git: bool = True) -> Optional[Dict]:
    ident = dir_identity(path)
    if ident is None:
        return None
    if with_git:
        ident.update(git_identity(path))
    ident["updated"] = _now()
    return ident


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─── Registry file ─────────────────────────────────────────────────────────

def empty_registry() -> Dict:
    return {"version": REGISTRY_VERSION, "projects": {}, "declined": []}


def load_registry(claude_dir: Path) -> Dict:
    """Load the registry; a missing or unreadable file yields an empty one.

    An unreadable file is flagged so save_registry() moves it aside instead of
    overwriting it.
    """
    path = registry_path(claude_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_registry()
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        reg = empty_registry()
        reg["_unreadable"] = True
        return reg
    if not isinstance(data.get("declined"), list):
        data["declined"] = []
    # Keys are NFC, as Claude Code's paths are (older files may hold NFD keys).
    projects: Dict = {}
    for path, entry in data["projects"].items():
        key = unicodedata.normalize("NFC", path)
        if key not in projects or entry.get("updated", "") > projects[key].get("updated", ""):
            projects[key] = entry
    data["projects"] = projects
    return data


def save_registry(claude_dir: Path, reg: Dict) -> None:
    path = registry_path(claude_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if reg.pop("_unreadable", False) and path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.replace(path, path.with_name(f"registry.json.unreadable-{stamp}"))
    out = {
        "version": REGISTRY_VERSION,
        "projects": dict(sorted(reg["projects"].items())),
        "declined": reg.get("declined", []),
    }
    atomic_write(path, (json.dumps(out, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def is_declined(reg: Dict, old: str, new: str) -> bool:
    """Was moving `old`'s data to `new` refused (or undone with restore)?"""
    return any(d.get("from") == old and d.get("to") == new for d in reg.get("declined", []))


def decline(reg: Dict, old: str, new: str) -> None:
    if not is_declined(reg, old, new):
        reg["declined"].append({"from": old, "to": new, "at": _now()})


def rekey(reg: Dict, old: str, new: str) -> int:
    """Move registry entries for `old` and its subfolders to `new`.

    Identity is re-read from disk for the new paths (a cross-volume move gives
    the folder a new inode); git details are kept. Returns entries moved.
    """
    from claudepath.updaters import remap_path

    projects = reg["projects"]
    moved = 0
    for path in sorted(projects):
        target = remap_path(path, old, new)
        if target is None or target == path:
            continue
        entry = projects.pop(path)
        ident = dir_identity(target)
        if ident is not None:
            entry.update(ident)
        entry["updated"] = _now()
        existing = projects.get(target)
        projects[target] = dict(entry, **existing) if existing else entry
        moved += 1
    reg["declined"] = [
        d for d in reg.get("declined", []) if not (d.get("from") == old and d.get("to") == new)
    ]
    return moved


# ─── Known project paths ───────────────────────────────────────────────────

def known_project_paths(claude_dir: Path, claude_json: Optional[Path], quick: bool = False) -> List[str]:
    """Every project path Claude Code has data for, existing or not.

    Sources: the cwd recorded in session files (one per project dir when
    `quick`), sessions-index.json (older versions), and the keys of the
    "projects" map in ~/.claude.json.
    """
    from claudepath.scanner import project_paths_in_dir

    paths: List[str] = []
    seen = set()

    def add(p: Optional[str]) -> None:
        if isinstance(p, str) and p.startswith("/"):
            p = unicodedata.normalize("NFC", p)
            if p not in seen:
                seen.add(p)
                paths.append(p)

    projects = claude_dir / "projects"
    if projects.is_dir():
        for entry in sorted(projects.iterdir()):
            if entry.is_dir():
                for p in project_paths_in_dir(entry, limit=1 if quick else None):
                    add(p)
    for p in claude_json_project_keys(claude_json):
        add(p)
    return paths


def claude_json_project_keys(claude_json: Optional[Path]) -> List[str]:
    if claude_json is None or not claude_json.exists():
        return []
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    return [k for k in projects] if isinstance(projects, dict) else []


class SyncResult:
    def __init__(self):
        self.added: List[str] = []
        self.refreshed: List[str] = []
        self.changed: List[str] = []   # identity differs; left for auto to resolve
        self.missing: List[str] = []   # path no longer exists


def sync_registry(
    reg: Dict,
    paths: Iterable[str],
    with_git: bool = True,
) -> SyncResult:
    """Record identity for every existing path in `paths` (in place).

    New paths are added. Known paths whose identity still matches get fresh git
    details. A known path whose folder has a different identity now is left
    alone: its recorded inode may be the only link to where the folder went,
    and `claudepath auto` resolves it on the next launch there.
    """
    result = SyncResult()
    projects = reg["projects"]
    for path in paths:
        ident = dir_identity(path)
        if ident is None:
            result.missing.append(path)
            continue
        entry = projects.get(path)
        if entry is None:
            entry = dict(ident)
            if with_git:
                entry.update(git_identity(path))
            entry["updated"] = _now()
            projects[path] = entry
            result.added.append(path)
        elif entry.get("dev") == ident["dev"] and entry.get("ino") == ident["ino"]:
            if with_git:
                entry.update(git_identity(path))
                entry.update(ident)
                entry["updated"] = _now()
                result.refreshed.append(path)
        else:
            result.changed.append(path)
    return result


def inode_index(reg: Dict) -> Dict[Tuple[int, int], List[str]]:
    index: Dict[Tuple[int, int], List[str]] = {}
    for path, entry in reg["projects"].items():
        if "dev" in entry and "ino" in entry:
            index.setdefault((entry["dev"], entry["ino"]), []).append(path)
    return index
