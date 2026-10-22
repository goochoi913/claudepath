"""
Finding and rescuing orphaned Claude Code projects (`claudepath scan`,
`claudepath status`): session directories whose folder no longer exists at
the recorded path.

A rescue target is found under a root directory, in order of confidence:
  1. the directory with the inode the registry recorded for the orphan, the
     same folder as confirmed by its birth time (macOS) or git root commit;
  2. otherwise -- or if nothing confirms the inode, which Linux reuses -- the
     only directory under the root with the same name, unless its git history
     contradicts the registry: a guess, to be confirmed.
Targets must be one-to-one: two orphans claiming one folder match neither.
"""

import collections
import os
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from claudepath.encoder import encode_path
from claudepath.registry import claude_json_project_keys, git_identity, identity_match
from claudepath.scanner import project_paths_in_dir

# Never descended into while indexing a root: caches, dependencies, VCS data.
PRUNE = {
    "node_modules", ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle", ".cache", ".npm",
    ".Trash", "Pods", "DerivedData", ".next", ".turbo",
}
MAX_DEPTH = 8


class Orphan:
    def __init__(self, path: str, project_dir: Path, sessions: int, entry: Optional[Dict]):
        self.path = path
        self.project_dir = project_dir
        self.sessions = sessions
        self.entry = entry


class Match:
    def __init__(self, orphan: Orphan, target: str, kind: str, note: str = ""):
        self.orphan = orphan
        self.target = target
        self.kind = kind  # "inode" (certain) or "name" (needs confirmation)
        self.note = note


def find_orphans(claude_dir: Path, claude_json: Optional[Path], reg: Dict) -> Tuple[List[Orphan], List[Path]]:
    """Return (orphans, unknown): project dirs whose path is gone, and project
    dirs whose path cannot be determined at all."""
    projects = claude_dir / "projects"
    if not projects.is_dir():
        return [], []
    by_slug: Dict[str, List[str]] = {}
    for p in claude_json_project_keys(claude_json) + list(reg["projects"]):
        by_slug.setdefault(encode_path(p), []).append(p)

    orphans, unknown = [], []
    for d in sorted(projects.iterdir()):
        if not d.is_dir():
            continue
        paths = project_paths_in_dir(d) or by_slug.get(d.name, [])
        if not paths:
            unknown.append(d)
            continue
        path = unicodedata.normalize("NFC", paths[0])
        if os.path.exists(path):
            continue
        sessions = sum(1 for _ in d.glob("*.jsonl"))
        orphans.append(Orphan(path, d, sessions, reg["projects"].get(path)))
    return orphans, unknown


def index_tree(root: str, claude_dir: Path, max_depth: int = MAX_DEPTH):
    """Walk `root` (not following symlinks) and index its directories.

    Returns ({(dev, ino): (path, stat)}, {basename: [paths]}).
    """
    by_inode: Dict[Tuple[int, int], Tuple[str, os.stat_result]] = {}
    by_name: Dict[str, List[str]] = collections.defaultdict(list)
    home = str(Path.home())
    skip = {os.path.realpath(claude_dir)}

    root = os.path.realpath(root)
    try:
        st = os.stat(root)
        by_inode[(st.st_dev, st.st_ino)] = (unicodedata.normalize("NFC", root), st)
        by_name[unicodedata.normalize("NFC", os.path.basename(root))].append(
            unicodedata.normalize("NFC", root)
        )
    except OSError:
        return by_inode, by_name

    stack = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name in PRUNE or (e.name == "Library" and path == home):
                    continue
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    est = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if e.path in skip:
                    continue
                full = unicodedata.normalize("NFC", e.path)
                by_inode[(est.st_dev, est.st_ino)] = (full, est)
                by_name[unicodedata.normalize("NFC", e.name)].append(full)
                if depth + 1 < max_depth:
                    stack.append((e.path, depth + 1))
    return by_inode, dict(by_name)


def match_orphans(
    orphans: List[Orphan],
    by_inode: Dict,
    by_name: Dict[str, List[str]],
) -> Tuple[List[Match], List[Tuple[Orphan, str]]]:
    """Pair orphans with rescue targets. Returns (matches, unmatched-with-reason).

    Subfolder projects of a matched orphan are left out: migrating the parent
    carries them along.
    """
    proposals: List[Match] = []
    unmatched: List[Tuple[Orphan, str]] = []
    git_cache: Dict[str, Dict] = {}

    for o in sorted(orphans, key=lambda o: len(o.path)):
        if any(o.path.startswith(m.orphan.path + "/") for m in proposals):
            continue
        e = o.entry or {}
        if "ino" in e:
            hit = by_inode.get((e.get("dev"), e["ino"]))
            if hit is not None:
                git = None
                if e.get("git_root") and (e.get("birth") is None
                                          or getattr(hit[1], "st_birthtime", None) is None):
                    git = git_cache.setdefault(hit[0], git_identity(hit[0]))
                match = identity_match(e, hit[1], git)
                if match == "same":
                    proposals.append(Match(o, hit[0], "inode", "same folder (inode)"))
                    continue
                if match == "unconfirmed":
                    proposals.append(Match(o, hit[0], "name", "same inode number, nothing to confirm it"))
                    continue
        name = unicodedata.normalize("NFC", os.path.basename(o.path))
        candidates = [p for p in by_name.get(name, []) if p != o.path]
        if not candidates:
            unmatched.append((o, f"no folder named '{name}' under the scanned root"))
            continue
        if len(candidates) > 1:
            unmatched.append((o, f"{len(candidates)} folders named '{name}'; ambiguous"))
            continue
        target = candidates[0]
        note = "same folder name"
        if e.get("git_root"):
            if target not in git_cache:
                git_cache[target] = git_identity(target)
            root = git_cache[target].get("git_root")
            if root and root != e["git_root"]:
                unmatched.append((o, f"'{target}' has a different git history"))
                continue
            if root:
                note = "same folder name and git history"
        proposals.append(Match(o, target, "name", note))

    claims = collections.Counter(m.target for m in proposals)
    matches = []
    for m in proposals:
        if claims[m.target] == 1:
            matches.append(m)
        else:
            unmatched.append((m.orphan, f"'{m.target}' is claimed by several orphans"))
    return matches, unmatched


def locate_inode(dev: int, ino: int) -> Optional[str]:
    """Where a directory with this device+inode lives now (macOS only).

    Uses the /.vol filesystem and F_GETPATH; returns None elsewhere or if the
    inode no longer exists.
    """
    if sys.platform != "darwin":
        return None
    try:
        fd = os.open(f"/.vol/{dev}/{ino}", os.O_RDONLY)
    except OSError:
        return None
    try:
        import fcntl

        buf = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", 50), bytes(1024))
        return unicodedata.normalize("NFC", buf.split(b"\0", 1)[0].decode("utf-8", "surrogateescape"))
    except OSError:
        return None
    finally:
        os.close(fd)
