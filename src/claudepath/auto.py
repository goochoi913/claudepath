"""
`claudepath auto` -- runs before every `claude` launch (see `claudepath install`).

Recognises a project folder that was moved or renamed and brings its Claude
Code data along, before Claude Code looks for it:

  strong match  a registry entry's recorded device+inode now belongs to the
                current directory (or one of its parents), the entry's path no
                longer refers to that folder, and the birth time (macOS) or the
                git root commit confirms it is the same folder: it moved or was
                renamed. Migrate without asking. An inode with nothing to
                confirm it (Linux reuses inode numbers) is only a guess, and is
                handled like a weak match.
  weak match    the current directory has no sessions yet, and exactly one
                registry entry whose path is gone has the same git root commit
                (a move to another volume gets a new inode). Ask y/N on a TTY,
                remember a "no", and never prompt otherwise.

Anything else changes nothing; the entry for the current directory is just
refreshed. Output goes to stderr and only appears when something happens --
stdout stays clean for `claude -p ... | jq`. Errors are reported in one line
and never stop the launch.

The common case (nothing moved, folder already known) reads one small JSON
file and makes no subprocess calls or writes.
"""

import os
import signal
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from claudepath import registry as reg_mod


def _say(msg: str) -> None:
    print(f"claudepath: {msg}", file=sys.stderr)


def _print_help() -> None:
    print(
        """claudepath auto — let Claude Code sessions follow moved/renamed folders

USAGE
  claudepath auto [--dry-run] [--verbose] [--claude-dir <dir>]

Meant to run right before `claude` (installed by `claudepath install`).
Silent unless it moves something; never blocks the launch.

OPTIONS
  --dry-run        Report what would happen; change and ask nothing
  --verbose, -v    Explain the decision even when nothing happens
  --claude-dir     Override the Claude data directory (default: ~/.claude)"""
    )


def main(argv: List[str]) -> int:
    dry_run = verbose = False
    claude_dir: Optional[Path] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            _print_help()
            return 0
        if arg == "--dry-run":
            dry_run = True
        elif arg in ("--verbose", "-v"):
            verbose = True
        elif arg == "--claude-dir" and i + 1 < len(argv):
            i += 1
            claude_dir = Path(argv[i]).expanduser()
        else:
            _say(f"auto: ignoring unknown option {arg}")
        i += 1
    try:
        return run(claude_dir=claude_dir, dry_run=dry_run, verbose=verbose)
    except KeyboardInterrupt:
        # Die by SIGINT so the calling shell function stops too: Ctrl-C at the
        # prompt cancels the launch, as it would anywhere else.
        sys.stderr.write("\n")
        sys.stderr.flush()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
        return 130
    except Exception as e:  # never block claude
        _say(f"auto skipped: {e}")
        return 1


def run(claude_dir: Optional[Path] = None, dry_run: bool = False, verbose: bool = False) -> int:
    from claudepath.scanner import find_claude_dir

    try:
        cwd_raw = os.getcwd()
    except OSError:
        return 0
    if claude_dir is None:
        claude_dir = find_claude_dir()
    if not claude_dir.is_dir():
        if verbose:
            _say(f"{claude_dir} does not exist; nothing to do")
        return 0

    cwd = reg_mod.claude_project_path(cwd_raw)
    st = os.stat(cwd_raw)
    first_run = not reg_mod.registry_path(claude_dir).exists()
    # A migration that died half way left this marker: revert it now.
    interrupted = (reg_mod.registry_dir(claude_dir) / "backups" / "in-progress").exists()
    reg = reg_mod.load_registry(claude_dir)

    entry = reg["projects"].get(cwd)
    known = entry is not None and reg_mod.same_identity(entry, st) and "git_root" in entry
    strong = [
        (old, here) for old, here, _ in _strong_candidates(reg, cwd_raw)
        if not reg_mod.is_declined(reg, old, here) and _moved_away(old, reg["projects"][old])
    ]
    weak = not strong and _weak_possible(reg, claude_dir, cwd, cwd_raw)
    if known and not first_run and not interrupted and not strong and not weak:
        if verbose:
            _say(f"{cwd} is known and nothing moved")
        return 0

    if dry_run:
        return _act(claude_dir, cwd, cwd_raw, first_run, dry_run, verbose)
    # Another claudepath may be mid-migration (say, a second terminal launching
    # claude in the same moved folder): wait for it rather than race it.
    with reg_mod.registry_lock(claude_dir, timeout=10.0):
        return _act(claude_dir, cwd, cwd_raw, first_run, dry_run, verbose)


# ─── Decisions ─────────────────────────────────────────────────────────────

def _strong_candidates(reg: Dict, cwd_raw: str) -> List[Tuple[str, str, os.stat_result]]:
    """Registry paths whose recorded inode is the current directory or one of
    its parents, under another name. Returns (old path, current path, stat),
    outermost first."""
    index = reg_mod.inode_index(reg)
    found = []
    p = cwd_raw
    while True:
        try:
            s = os.stat(p)
        except OSError:
            s = None
        if s is not None:
            here = unicodedata.normalize("NFC", p)
            for old in index.get((s.st_dev, s.st_ino), []):
                if old != here:
                    found.append((old, here, s))
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    found.sort(key=lambda c: len(c[1]))
    return found


def _weak_possible(reg: Dict, claude_dir: Path, cwd: str, cwd_raw: str) -> bool:
    """Cheap pre-check (no subprocess) before computing git identity."""
    if reg_mod.has_sessions(claude_dir, cwd) or not reg_mod.inside_git_worktree(cwd_raw):
        return False
    return any(
        e.get("git_root") and p != cwd and _gone(p) for p, e in reg["projects"].items()
    )


def _moved_away(old: str, entry: Dict) -> bool:
    """True if `old` no longer refers to the folder the entry describes."""
    try:
        s = os.stat(old)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False  # can't tell: assume it is still there
    if (s.st_dev, s.st_ino) != (entry.get("dev"), entry.get("ino")):
        return True  # something else lives at the old path now
    # Same folder. Still a valid name for it? Not after a case-only rename on
    # a case-insensitive volume, where the old spelling still resolves.
    canonical = reg_mod.canonical_project_path(old)
    return canonical is not None and canonical != old


def _gone(path: str) -> bool:
    """The path doesn't exist -- and not merely because its volume is unmounted."""
    if os.path.lexists(path):
        return False
    parent = os.path.dirname(path)
    while parent != "/" and not os.path.isdir(parent):
        parent = os.path.dirname(parent)
    mount_roots = {"/Volumes", "/media", "/mnt", "/run/media"}
    return parent not in mount_roots and os.path.dirname(parent) not in {"/media", "/run/media"}


def _claude_processes_in(*paths: str) -> List[int]:
    """PIDs of running Claude Code processes whose cwd is inside any of `paths`.

    A session still running in a moved folder keeps appending to the old
    project's files; migrating under it would split that session.
    """
    import re
    import subprocess

    def inside(p: str) -> bool:
        p = unicodedata.normalize("NFC", p)
        return any(p == x or p.startswith(x + "/") for x in paths)

    pids: List[int] = []
    if os.path.isdir("/proc/self"):
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/comm", encoding="utf-8") as f:
                    if f.read().strip() != "claude":
                        continue
                if inside(os.readlink(f"/proc/{d}/cwd")):
                    pids.append(int(d))
            except OSError:
                continue
        return pids

    # BSD pgrep leaves out its own ancestors unless given -a: a Claude session
    # that (indirectly) started this process must still count.
    pgrep = ["pgrep", "-a", "-x", "claude"] if sys.platform == "darwin" else ["pgrep", "-x", "claude"]
    # lsof escapes non-ASCII bytes as \xNN outside a UTF-8 locale.
    env = dict(os.environ, LC_ALL="en_US.UTF-8")
    try:
        found = subprocess.run(pgrep, capture_output=True, text=True, timeout=2).stdout.split()
        if not found:
            return []
        out = subprocess.run(
            ["lsof", "-a", "-p", ",".join(found), "-d", "cwd", "-Fn"],
            capture_output=True, timeout=2, env=env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pid = None
    for raw in out.splitlines():
        if raw.startswith(b"p"):
            pid = int(raw[1:])
        elif raw.startswith(b"n") and pid is not None:
            name = re.sub(rb"\\x([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]), raw[1:])
            if inside(name.decode("utf-8", "surrogateescape")):
                pids.append(pid)
    return pids


# ─── Acting ────────────────────────────────────────────────────────────────

def _act(claude_dir: Path, cwd: str, cwd_raw: str, first_run: bool, dry_run: bool, verbose: bool) -> int:
    from claudepath.migrate import migrate, recover_interrupted, shorten
    from claudepath.scanner import find_claude_json

    claude_json = find_claude_json(claude_dir)
    if not dry_run:
        for journal_dir in recover_interrupted(claude_dir):
            _say(f"rolled back an interrupted migration (backup {journal_dir.name})")
    reg = reg_mod.load_registry(claude_dir)  # re-read under the lock
    changed = False
    git_cache: Dict[str, Dict] = {}

    def git_for(path: str) -> Dict:
        if path not in git_cache:
            git_cache[path] = reg_mod.git_identity(path)
        return git_cache[path]

    def move(old: str, here: str, reason: str, heading: str) -> bool:
        running = _claude_processes_in(here, old)
        if running:
            _say(
                f"{shorten(old)} was moved to {shorten(here)}, but a Claude session "
                f"(pid {running[0]}) is still running in it. Its sessions will follow "
                "on the next launch after that session exits."
            )
            return False
        if dry_run:
            _say(f"[dry run] would move sessions {shorten(old)} → {shorten(here)}")
            print(migrate(old, here, claude_dir, claude_json, dry_run=True).summary(), file=sys.stderr)
            return True
        result = migrate(old, here, claude_dir, claude_json, reason=reason, verbose=verbose)
        if result.changed_anything or verbose:
            _say(f"{heading}: {result.one_line()}")
        return True

    if first_run:
        # No registry yet (sync never ran): record what exists, quickly.
        paths = reg_mod.known_project_paths(claude_dir, claude_json, quick=True)
        reg_mod.sync_registry(reg, paths, with_git=False)
        changed = True

    acted = False
    for old, here, s in _strong_candidates(reg, cwd_raw):
        entry = reg["projects"].get(old)
        if entry is None or reg_mod.is_declined(reg, old, here) or not _moved_away(old, entry):
            continue
        # Git only matters when birth times can't decide (one of them unknown).
        needs_git = entry.get("git_root") and (
            entry.get("birth") is None or getattr(s, "st_birthtime", None) is None)
        match = reg_mod.identity_match(entry, s, git_for(here) if needs_git else None)
        if match == "different":
            continue
        if match == "unconfirmed":
            # Same inode number, nothing to confirm it: possibly a recycled
            # inode of a deleted folder. Ask, as for a weak match.
            if not dry_run and not _interactive():
                continue
            sessions = sum(1 for _ in reg_mod.project_dir_for(claude_dir, old).glob("*.jsonl"))
            if dry_run:
                _say(f"[dry run] would ask whether to move {sessions} session(s) from {shorten(old)}")
                continue
            if not _ask(old, sessions, "has the same inode number (with no creation time or git "
                                       "history to confirm it is the same folder)"):
                reg_mod.decline(reg, old, here)
                changed = True
                _say("OK, won't ask again for this folder.")
                continue
        if not move(old, here, "auto: folder moved", "sessions followed your folder"):
            return 0
        acted = True
        reg = reg_mod.load_registry(claude_dir)

    if not acted and not reg_mod.has_sessions(claude_dir, cwd):
        old = _weak_candidate(reg, claude_dir, cwd, git_for)
        if old is not None:
            sessions = sum(1 for _ in reg_mod.project_dir_for(claude_dir, old).glob("*.jsonl"))
            root = str(git_for(cwd).get("git_root"))[:10]
            if dry_run:
                _say(f"[dry run] would ask whether to move {sessions} session(s) from {shorten(old)}")
            elif _interactive():
                if _ask(old, sessions, f"has the same git history (root commit {root}) "
                                       "and the old path is gone"):
                    if move(old, cwd, "auto: confirmed copy", "sessions moved"):
                        reg = reg_mod.load_registry(claude_dir)
                else:
                    reg_mod.decline(reg, old, cwd)
                    changed = True
                    _say("OK, won't ask again for this folder.")

    # Refresh the entry for the current directory.
    st = os.stat(cwd_raw)
    entry = reg["projects"].get(cwd)
    if entry is None or not reg_mod.same_identity(entry, st) or "git_root" not in entry:
        refreshed = dict(entry or {})
        identity_changed = entry is None or not reg_mod.same_identity(entry, st)
        refreshed.pop("birth", None)
        refreshed.update(reg_mod.identity_from_stat(st))
        if identity_changed or "git_root" not in refreshed:
            refreshed.update(git_for(cwd))
        refreshed["updated"] = reg_mod._now()
        reg["projects"][cwd] = refreshed
        changed = True
        if verbose:
            _say(f"{'[dry run] would register' if dry_run else 'registered'} {cwd}")

    if changed and not dry_run:
        reg_mod.save_registry(claude_dir, reg)
    return 0


def _weak_candidate(reg: Dict, claude_dir: Path, cwd: str, git_for) -> Optional[str]:
    orphans = [
        p for p, e in reg["projects"].items() if p != cwd and e.get("git_root") and _gone(p)
    ]
    if not orphans:
        return None
    here = git_for(cwd)
    root = here.get("git_root")
    if not root:
        return None
    prefix = here.get("git_prefix") or ""
    candidates = [
        p for p in orphans
        if reg["projects"][p]["git_root"] == root
        and (reg["projects"][p].get("git_prefix") or "") == prefix
        and reg_mod.project_dir_for(claude_dir, p).is_dir()
    ]
    if len(candidates) > 1 and here.get("git_remote"):
        same_remote = [p for p in candidates if reg["projects"][p].get("git_remote") == here["git_remote"]]
        if same_remote:
            candidates = same_remote
    if len(candidates) != 1 or reg_mod.is_declined(reg, candidates[0], cwd):
        return None
    return candidates[0]


def _interactive() -> bool:
    """A person is at the terminal: both the prompt and the answer use it."""
    return sys.stdin.isatty() and sys.stderr.isatty()


def _ask(old: str, sessions: int, evidence: str) -> bool:
    from claudepath.migrate import shorten

    sys.stderr.write(
        f"claudepath: this folder may be {shorten(old)}, moved: it {evidence}.\n"
        f"  {sessions} Claude Code session(s) belong to the old path. Move them here? [y/N] "
    )
    sys.stderr.flush()
    answer = sys.stdin.readline()
    if not answer:  # EOF
        sys.stderr.write("\n")
    return answer.strip().lower() in ("y", "yes")
