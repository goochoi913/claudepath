"""
Migration engine for the automatic mode (`claudepath auto` and `scan`).

Moves everything Claude Code keys by project path from OLD to NEW:

  ~/.claude/projects/<slug>/  for OLD and for every project started in a
                              subfolder of OLD: renamed, or merged into an
                              existing directory without overwriting anything
  session .jsonl files        only the top-level "cwd" field changes; every
                              other byte and the file mtimes are preserved
  sessions-index.json         (older Claude Code versions)
  ~/.claude/history.jsonl     the "project" field
  ~/.claude.json              "projects" keys (old values win on merge) and
                              githubRepoPaths
  usage-data/session-meta     (older Claude Code versions)
  registry.json               claudepath's identity records

Every file is saved to a Journal before it changes. If any step fails the
journal is replayed backwards, so nothing is left half-migrated. Callers hold
registry.registry_lock() while migrating.
"""

import filecmp
import json
import os
import signal
import sys
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Generator, List, Optional, Tuple

from claudepath.backup import IN_PROGRESS, Journal, _copy_back, clear_marker, get_journal_base
from claudepath.encoder import _replace_non_alnum, encode_path
from claudepath.fsutil import AtomicFile, atomic_write, file_lock
from claudepath.registry import (
    claude_json_project_keys,
    decline,
    load_registry,
    make_entry,
    registry_dir,
    registry_path,
    rekey,
    save_registry,
)
from claudepath.scanner import find_claude_dir, find_claude_json, project_paths_in_dir
from claudepath.updaters import (
    json_bytes,
    merge_sessions_index,
    remap_path,
    rewrite_line_field,
    rewrite_history_projects,
    rewrite_jsonl_field,
    update_claude_json,
    update_sessions_index,
    update_usage_data,
)


class MigrationError(Exception):
    """Raised when a migration cannot be planned or had to be rolled back."""


class MigrationResult:
    """What a migration changed (or would change, for a dry run)."""

    def __init__(self, old_path: str, new_path: str, dry_run: bool):
        self.old_path = old_path
        self.new_path = new_path
        self.dry_run = dry_run
        self.dir_moves: List[Tuple[str, str, str]] = []  # (old path, new path, how)
        self.sessions = 0
        self.files_rewritten = 0
        self.cwd_lines = 0
        self.duplicates = 0
        self.conflicts: List[str] = []
        self.sessions_index = 0
        self.history_lines = 0
        self.config_keys: List[Tuple[str, str]] = []
        self.repo_paths = 0
        self.usage_data = 0
        self.registry_entries = 0
        self.backup_path: Optional[Path] = None

    @property
    def changed_anything(self) -> bool:
        """True if Claude Code data changed (registry-only updates don't count)."""
        return bool(
            self.dir_moves or self.cwd_lines or self.sessions_index or self.history_lines
            or self.config_keys or self.repo_paths or self.usage_data
        )

    def one_line(self) -> str:
        bits = []
        if self.sessions:
            bits.append(f"{self.sessions} session{'s' if self.sessions != 1 else ''}")
        subs = sum(1 for _, _, how in self.dir_moves) - (1 if self._main_dir_moved() else 0)
        if subs > 0:
            bits.append(f"{subs} subfolder project{'s' if subs != 1 else ''}")
        if not self.sessions and self.config_keys:
            bits.append("settings")
        if self.conflicts:
            bits.append(f"{len(self.conflicts)} conflicting file(s) kept side by side")
        if self.backup_path:
            bits.append(f"backup {self.backup_path.name}")
        detail = f" ({'; '.join(bits)})" if bits else ""
        return f"{shorten(self.old_path)} → {shorten(self.new_path)}{detail}"

    def _main_dir_moved(self) -> bool:
        return any(old == self.old_path for old, _, _ in self.dir_moves)

    def summary(self) -> str:
        prefix = "[DRY RUN] Would have: " if self.dry_run else ""
        lines = []
        for old, new, how in self.dir_moves:
            verb = {"rename": "renamed", "merge": "merged", "inplace": "kept (same slug)"}[how]
            lines.append(f"{prefix}{verb} project data {shorten(old)} → {shorten(new)}")
        if self.cwd_lines:
            lines.append(
                f"{prefix}updated cwd in {self.files_rewritten} session file(s) "
                f"({self.cwd_lines} line(s))"
            )
        if self.duplicates:
            lines.append(f"{prefix}dropped {self.duplicates} identical duplicate file(s)")
        for c in self.conflicts:
            lines.append(f"{prefix}kept both versions of {c}")
        if self.sessions_index:
            lines.append(f"{prefix}updated sessions-index.json")
        if self.history_lines:
            lines.append(f"{prefix}updated {self.history_lines} line(s) in history.jsonl")
        if self.config_keys:
            lines.append(
                f"{prefix}moved {len(self.config_keys)} project setting(s) in ~/.claude.json"
            )
        if self.repo_paths:
            lines.append(f"{prefix}updated {_plural(self.repo_paths, 'githubRepoPaths entry', 'githubRepoPaths entries')}")
        if self.usage_data:
            lines.append(f"{prefix}updated {self.usage_data} usage-data file(s)")
        if self.registry_entries:
            lines.append(f"{prefix}updated {_plural(self.registry_entries, 'registry entry', 'registry entries')}")
        if self.backup_path:
            lines.append(f"backup saved to: {self.backup_path}")
        if not lines:
            lines.append("nothing to update")
        return "\n".join(f"  - {l}" for l in lines)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def shorten(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _normalize(path: str) -> str:
    path = unicodedata.normalize("NFC", path)
    return path.rstrip("/") or "/"


# ─── Discovery ─────────────────────────────────────────────────────────────

def find_affected_dirs(claude_dir: Path, old: str, known_paths: List[str]) -> List[Tuple[Path, str]]:
    """Find ~/.claude/projects/ directories belonging to `old` or a subfolder.

    Returns (directory, project path) pairs. A directory's project path is
    read from its session files; for directories without sessions (e.g. only
    memory/) it must be confirmed by a known path (~/.claude.json key or
    registry entry) whose slug is that directory's name -- a slug that merely
    starts with OLD's slug ("-a-proj-v2" vs "-a-proj") is not enough.
    """
    projects = claude_dir / "projects"
    if not projects.is_dir():
        return []
    main = encode_path(old)
    prefix = _replace_non_alnum(old)[:200]
    by_slug: dict = {}
    for p in known_paths:
        by_slug.setdefault(encode_path(p), []).append(p)

    found: List[Tuple[Path, str]] = []
    for d in sorted(projects.iterdir()):
        if not d.is_dir() or not d.name.startswith(prefix):
            continue
        paths = project_paths_in_dir(d)
        inside = [p for p in paths if remap_path(p, old, old) is not None]
        foreign = [p for p in paths if p not in inside]
        if d.name == main:
            if foreign:
                raise MigrationError(
                    f"{d} also holds sessions from {foreign[0]}; "
                    "not moving it automatically (use 'claudepath remap')"
                )
            found.append((d, old))
            continue
        if not paths:
            # No sessions to tell whose directory this is: trust the known
            # paths with this slug only if all of them are under OLD.
            known = by_slug.get(d.name, [])
            inside = [p for p in known if remap_path(p, old, old) is not None]
            if len(inside) != len(known):
                continue
        elif foreign and inside:
            raise MigrationError(
                f"{d} holds sessions from both {inside[0]} and {foreign[0]}; "
                "not moving it automatically"
            )
        subs = sorted({unicodedata.normalize("NFC", p) for p in inside if p != old})
        if subs:
            found.append((d, subs[0]))
    return found


def _usage_data_files(claude_dir: Path, old: str) -> List[Path]:
    meta_dir = claude_dir / "usage-data" / "session-meta"
    if not meta_dir.exists():
        return []
    files = []
    for f in meta_dir.glob("*.json"):
        try:
            pp = json.loads(f.read_text(encoding="utf-8")).get("project_path", "")
        except (ValueError, OSError, AttributeError):
            continue
        if isinstance(pp, str) and remap_path(pp, old, old) is not None:
            files.append(f)
    return files


# ─── Migration ─────────────────────────────────────────────────────────────

def migrate(
    old_path: str,
    new_path: str,
    claude_dir: Optional[Path] = None,
    claude_json: Optional[Path] = None,
    dry_run: bool = False,
    reason: str = "manual",
    verbose: bool = False,
) -> MigrationResult:
    """Move all Claude Code data for `old_path` (and its subfolders) to `new_path`.

    `new_path` is where the folder is now; this never touches the folder
    itself. Raises MigrationError if the move cannot be planned safely or
    failed (in which case every change has been rolled back).
    """
    if claude_dir is None:
        claude_dir = find_claude_dir()
    if claude_json is None:
        claude_json = find_claude_json(claude_dir)
    old = _normalize(old_path)
    new = _normalize(new_path)
    if old == new:
        raise MigrationError("Source and destination are the same path.")
    if "/" in (old, new):
        raise MigrationError("Refusing to migrate the filesystem root.")
    if new.startswith(old + "/") or old.startswith(new + "/"):
        raise MigrationError(f"{new} and {old} are inside one another.")

    reg = load_registry(claude_dir)
    known = claude_json_project_keys(claude_json) + list(reg["projects"])
    moves = []
    for src, path in find_affected_dirs(claude_dir, old, known):
        target = new + path[len(old):]
        moves.append((src, claude_dir / "projects" / encode_path(target), path, target))
    moves = _order_moves(moves)

    result = MigrationResult(old, new, dry_run)
    if dry_run:
        _preview(moves, claude_dir, claude_json, reg, old, new, result)
        return result

    journal = Journal(get_journal_base(claude_dir), reason, {"old_path": old, "new_path": new},
                      recoverable=True)
    result.backup_path = journal.dir
    try:
        with _signals_raise():
            _apply(moves, claude_dir, claude_json, old, new, journal, result, verbose)
    except BaseException as e:
        with _signals_ignored():
            problems = _revert(journal.manifest, journal.dir, claude_dir, claude_json)
            journal.finish("rolled back" if not problems else "rollback failed", error=repr(e))
        if isinstance(e, _Terminated):
            raise SystemExit(128 + e.signum)
        if not isinstance(e, Exception):
            raise
        if problems:
            raise MigrationError(
                f"Migration failed: {e}\nRollback hit problems: {'; '.join(problems)}\n"
                f"Backup: {journal.dir}"
            ) from e
        raise MigrationError(f"Migration failed: {e}\nAll changes were rolled back.") from e

    if journal.is_empty:
        journal.discard()
        result.backup_path = None
    else:
        journal.finish()
    return result


def _apply(moves, claude_dir: Path, claude_json: Path, old: str, new: str,
           journal: Journal, result: MigrationResult, verbose: bool) -> None:
    for src, dst, path, target in moves:
        how = _how(src, dst)
        if how == "merge":
            _check_merge_target(dst, target)
        if verbose:
            print(f"  {how}: {src.name} -> {dst.name}", file=sys.stderr)
        files = _move_project_dir(src, dst, how, path, target, journal, result)
        result.dir_moves.append((path, target, how))
        for f in files:
            n = rewrite_jsonl_field(f, "cwd", old, new, before_write=journal.save_linked)
            if n:
                result.files_rewritten += 1
                result.cwd_lines += n
        index = dst / "sessions-index.json"
        if index.exists():
            journal.save(index)
            result.sessions_index += update_sessions_index(index, path, target, dst.name)

    result.history_lines = rewrite_history_projects(
        claude_dir / "history.jsonl", old, new, before_write=journal.save_linked
    )
    result.config_keys, result.repo_paths = update_claude_json(
        claude_json, old, new, before_write=journal.save
    )
    for f in _usage_data_files(claude_dir, old):
        journal.save(f)
    result.usage_data = update_usage_data(claude_dir, old, new)

    reg = load_registry(claude_dir)
    n = rekey(reg, old, new)
    if new not in reg["projects"]:
        entry = make_entry(new, with_git=False)
        if entry is not None:
            reg["projects"][new] = entry
            n += 1
    if n:
        journal.save(registry_path(claude_dir))
        save_registry(claude_dir, reg)
    result.registry_entries = n


def _how(src: Path, dst: Path) -> str:
    """Decided right before each move, after earlier moves have run."""
    if dst == src:
        return "inplace"
    if not dst.exists() or os.path.samefile(src, dst):
        # samefile: a case-only rename on a case-insensitive volume, where the
        # new slug already "exists" because it is the same directory.
        return "rename"
    return "merge"


def _dir_key(p: Path):
    try:
        st = os.stat(p)
        return (st.st_dev, st.st_ino)
    except OSError:
        return str(p)


def _order_moves(moves: List[tuple]) -> List[tuple]:
    """Order directory moves so a directory that another move lands in is
    moved away first.

    Renaming app -> app-web makes "-...-app-web", the directory of the
    subfolder project app/web, the destination of app's own data: app/web's
    data has to leave for "-...-app-web-web" before app's arrives, or the two
    would be merged and then carried off together.
    """
    by_src = {_dir_key(m[0]): m for m in moves}
    dsts = [str(m[1]) for m in moves]
    if len(set(dsts)) != len(dsts):
        raise MigrationError("Two project directories would land in the same place.")
    ordered: List[tuple] = []
    state: Dict = {}

    def visit(m: tuple) -> None:
        key = _dir_key(m[0])
        if state.get(key) == "done":
            return
        if state.get(key) == "visiting":
            raise MigrationError("Project directories would swap names; not migrating automatically.")
        state[key] = "visiting"
        blocker = by_src.get(_dir_key(m[1]))
        if blocker is not None and blocker is not m:
            visit(blocker)
        state[key] = "done"
        ordered.append(m)

    for m in moves:
        visit(m)
    return ordered


def _check_merge_target(dst: Path, target: str) -> None:
    """Refuse to merge into a directory that holds another folder's sessions
    (a different path that happens to have the same slug, like my_app/my-app)."""
    others = [p for p in project_paths_in_dir(dst) if unicodedata.normalize("NFC", p) != target]
    if others:
        raise MigrationError(
            f"{dst.name} already holds sessions of {others[0]}, a different folder with the "
            "same Claude Code directory name; not merging automatically"
        )


class _Terminated(BaseException):
    def __init__(self, signum: int):
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


@contextmanager
def _signals_raise() -> Generator[None, None, None]:
    """Turn SIGTERM and SIGHUP (the terminal closing) into an exception during
    a migration, so that it is rolled back instead of left half done."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        raise _Terminated(signum)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for sig, h in previous.items():
            signal.signal(sig, h)


@contextmanager
def _signals_ignored() -> Generator[None, None, None]:
    """Let a rollback finish even if the terminal goes away meanwhile."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for sig, h in previous.items():
            signal.signal(sig, h)


def recover_interrupted(claude_dir: Path, claude_json: Optional[Path] = None) -> List[Path]:
    """Revert a migration (or undo) that never finished: a crash, kill -9, a
    power cut. Its journal left an in-progress marker behind.

    Call with registry_lock() held: then nothing is migrating, so a marker
    belongs to a process that died. Like an undo, this keeps whatever was
    written since -- the next launch may be days later.
    """
    base = get_journal_base(claude_dir)
    try:
        name = (base / IN_PROGRESS).read_text(encoding="utf-8").strip()
    except OSError:
        return []
    reverted = revert_interrupted(base / name, claude_dir, claude_json)
    clear_marker(base, name)
    return [base / name] if reverted else []


def revert_interrupted(journal_dir: Path, claude_dir: Path, claude_json: Optional[Path] = None) -> bool:
    """Revert one migration journal left "in progress"; False if it isn't one."""
    manifest_path = journal_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if manifest.get("status") not in ("in progress", "rollback failed") or not manifest.get("old_path"):
        return False
    if claude_json is None:
        claude_json = find_claude_json(claude_dir)
    with _signals_ignored():
        problems = _revert(manifest, journal_dir, claude_dir, claude_json)
        manifest["status"] = "rolled back (interrupted)" if not problems else "rollback failed"
        if problems:
            manifest["problems"] = problems
        atomic_write(manifest_path, json_bytes(manifest, indent=2) + b"\n")
    for op in manifest["ops"]:
        for key in ("path", "src", "dst"):
            if key in op:
                for tmp in Path(op[key]).parent.glob(".*.claudepath-tmp"):
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
    return True


def _move_project_dir(
    src: Path, dst: Path, how: str, old_p: str, new_p: str, journal: Journal, result: MigrationResult
) -> List[Path]:
    """Rename or merge one project directory; return the session files to rewrite."""
    if how == "inplace":
        return sorted(dst.rglob("*.jsonl"))
    if how == "rename":
        journal.moved(src, dst, files=[str(f.relative_to(src)) for f in src.rglob("*")
                                       if not f.is_dir() or f.is_symlink()])
        os.rename(src, dst)
        result.sessions += sum(1 for _ in dst.glob("*.jsonl"))
        return sorted(dst.rglob("*.jsonl"))
    return _merge_dirs(src, dst, old_p, new_p, journal, result)


def _merge_dirs(
    src: Path, dst: Path, old_p: str, new_p: str, journal: Journal, result: MigrationResult
) -> List[Path]:
    """Move every file from src into dst without overwriting anything.

    A file dst doesn't have is moved. An identical copy is dropped (it is in
    the backup). A different file with the same name is kept next to dst's as
    "<name>.claudepath-conflict-<timestamp><ext>".
    """
    if os.path.samefile(src, dst):
        raise MigrationError(f"refusing to merge {src} into itself")
    stamp = journal.dir.name
    src_index, dst_index = src / "sessions-index.json", dst / "sessions-index.json"
    if src_index.exists() and dst_index.exists():
        journal.save(dst_index)
        merge_sessions_index(dst_index, src_index, old_p, new_p, dst.name)
        journal.deleted(src_index)
        src_index.unlink()

    to_rewrite: List[Path] = []
    for f in sorted(src.rglob("*")):
        if f.is_dir() and not f.is_symlink():
            continue
        rel = f.relative_to(src)
        target = dst / rel
        _make_parents(target.parent, dst, journal)
        if not os.path.lexists(target):
            journal.moved(f, target)
            os.rename(f, target)
        elif os.path.samefile(f, target):
            raise MigrationError(f"{f} and {target} are the same file")
        elif f.is_file() and target.is_file() and filecmp.cmp(str(f), str(target), shallow=False):
            journal.deleted(f)
            f.unlink()
            result.duplicates += 1
            continue
        else:
            target = _conflict_name(target, stamp)
            journal.moved(f, target)
            os.rename(f, target)
            result.conflicts.append(f"{rel} (old copy: {target.name})")
        if target.suffix == ".jsonl":
            to_rewrite.append(target)
            if target.parent == dst:
                result.sessions += 1

    for d in sorted((p for p in src.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        src.rmdir()
    except OSError:
        pass
    return to_rewrite


def _make_parents(directory: Path, root: Path, journal: Optional[Journal]) -> None:
    missing = []
    d = directory
    while d != root and not d.exists():
        missing.append(d)
        d = d.parent
    for d in reversed(missing):
        d.mkdir()
        if journal is not None:
            journal.made_dir(d)


def _conflict_name(target: Path, stamp: str) -> Path:
    n = 1
    while True:
        tag = f".claudepath-conflict-{stamp}" + (f"-{n}" if n > 1 else "")
        candidate = target.with_name(target.stem + tag + target.suffix)
        if not os.path.lexists(candidate):
            return candidate
        n += 1


def _preview(moves, claude_dir: Path, claude_json: Path, reg: dict, old: str, new: str, result: MigrationResult) -> None:
    """Fill `result` with what migrate() would do, without writing anything."""
    vacated = set()
    for src, dst, path, target in moves:
        how = "rename" if _dir_key(dst) in vacated else _how(src, dst)
        vacated.add(_dir_key(src))
        result.dir_moves.append((path, target, how))
        result.sessions += sum(1 for _ in src.glob("*.jsonl"))
        if how == "merge":
            for f in src.rglob("*"):
                if f.is_file() and f.name != "sessions-index.json":
                    t = dst / f.relative_to(src)
                    if t.exists() and not filecmp.cmp(str(f), str(t), shallow=False):
                        result.conflicts.append(str(f.relative_to(src)))
        for f in src.rglob("*.jsonl"):
            n = rewrite_jsonl_field(f, "cwd", old, new, dry_run=True)
            if n:
                result.files_rewritten += 1
                result.cwd_lines += n
    result.history_lines = rewrite_history_projects(
        claude_dir / "history.jsonl", old, new, dry_run=True
    )
    result.config_keys, result.repo_paths = update_claude_json(claude_json, old, new, dry_run=True)
    result.usage_data = update_usage_data(claude_dir, old, new, dry_run=True)
    result.registry_entries = sum(1 for p in reg["projects"] if remap_path(p, old, new) is not None)


# ─── Reverting: rollback, recovery, undo ───────────────────────────────────
#
# One engine reverses a migration journal in all three cases. It never copies
# a stale backup over a file Claude Code keeps writing: session files get the
# inverse cwd rewrite in place (so lines appended since stay), and only the
# history lines and project settings the migration changed are reverted.

def _unit(rel: str) -> str:
    """The session (or other top-level entry) a project-dir path belongs to:
    "<id>.jsonl" and everything under "<id>/" are one session; "memory/" is
    one unit."""
    head = rel.split("/", 1)[0]
    return head[: -len(".jsonl")] if head.endswith(".jsonl") else head


def _files_in(d: Path) -> List[Path]:
    return [f for f in d.rglob("*") if not f.is_dir() or f.is_symlink()]


def _project_dir_of(path: Path, claude_dir: Path) -> Optional[Path]:
    projects = claude_dir / "projects"
    try:
        return projects / path.relative_to(projects).parts[0]
    except (ValueError, IndexError):
        return None


def _revert(manifest: Dict, backup_dir: Path, claude_dir: Path, claude_json: Path,
            journal: Optional[Journal] = None) -> List[str]:
    """Reverse a migration journal's steps, keeping newer data. Steps already
    reverted (or never done) are skipped, so this is safe to repeat. Returns
    a list of problems."""
    old, new = manifest["old_path"], manifest["new_path"]
    saved = {op["path"]: backup_dir / op["backup"] for op in manifest["ops"] if op["op"] == "save"}
    problems: List[str] = []
    for op in reversed(manifest["ops"]):
        try:
            _revert_op(op, backup_dir, old, new, claude_dir, claude_json, journal)
        except (OSError, MigrationError) as e:
            problems.append(f"{op['op']} {op.get('path') or op.get('dst')}: {e}")
    try:
        history = claude_dir / "history.jsonl"
        if str(history) in saved:
            _revert_history(history, saved[str(history)], old, new, journal)
        if str(claude_json) in saved:
            _revert_claude_json(claude_json, saved[str(claude_json)], old, new, journal)
        if str(registry_path(claude_dir)) in saved:
            reg = load_registry(claude_dir)
            if journal is not None:
                journal.save(registry_path(claude_dir))
            rekey(reg, new, old)
            if manifest.get("reason", "").startswith("undo"):
                # Reverting an undo: forget the "don't migrate again" it recorded.
                reg["declined"] = [d for d in reg["declined"]
                                   if not (d.get("from") == new and d.get("to") == old)]
            save_registry(claude_dir, reg)
    except (OSError, ValueError) as e:
        problems.append(f"shared files: {e}")
    return problems


def _revert_op(op: Dict, backup_dir: Path, old: str, new: str, claude_dir: Path,
               claude_json: Path, journal: Optional[Journal]) -> None:
    kind = op["op"]
    if kind == "move":
        _revert_move(op, claude_dir, journal, old, new)
    elif kind == "mkdir":
        try:
            os.rmdir(op["path"])
        except OSError:
            pass  # gone, or holds files written since: leave it
    elif kind == "create":
        try:
            os.unlink(op["path"])
        except FileNotFoundError:
            pass
    elif kind == "delete":
        path = Path(op["path"])
        if not os.path.lexists(path):
            _copy_back(backup_dir / op["backup"], path)
            if journal is not None:
                journal.created(path)
    elif kind == "save":
        path = Path(op["path"])
        if path.name == "history.jsonl" or path == claude_json or path.parent == registry_dir(claude_dir):
            return  # reverted separately, line by line / key by key
        if path.suffix == ".jsonl":
            if path.exists():
                rewrite_jsonl_field(path, "cwd", new, old,
                                    before_write=journal.save_linked if journal else None)
        elif path.exists():  # sessions-index.json, usage-data: metadata of older versions
            if journal is not None:
                journal.save(path)
            _copy_back(backup_dir / op["backup"], path)


def _revert_move(op: Dict, claude_dir: Path, journal: Optional[Journal], old: str, new: str) -> None:
    src, dst = Path(op["src"]), Path(op["dst"])  # the migration moved src to dst
    if not os.path.lexists(dst):
        return
    case_only = os.path.lexists(src) and os.path.samefile(src, dst)
    if os.path.lexists(src) and not case_only:
        raise MigrationError(f"cannot move {dst} back: {src} exists")
    files = op.get("files")
    newer: List[str] = []  # session files written since, inside units that go back
    if files is not None and not case_only and dst.is_dir() and not dst.is_symlink():
        listed = set(files)
        units = {_unit(rel) for rel in files}
        current = [str(f.relative_to(dst)) for f in _files_in(dst)]
        newer = [rel for rel in current
                 if rel not in listed and _unit(rel) in units and rel.endswith(".jsonl")]
        if any(_unit(rel) not in units for rel in current):
            # Sessions were started at the new path since: move back whole
            # units the migration brought (a session with its subagents,
            # memory/), and leave the newer ones where they are.
            for rel in current:
                if _unit(rel) in units:
                    target = src / rel
                    _make_parents(target.parent, claude_dir / "projects", journal)
                    if journal is not None:
                        journal.moved(dst / rel, target)
                    os.rename(dst / rel, target)
            for d in sorted((p for p in dst.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
                try:
                    d.rmdir()
                except OSError:
                    pass
            _rewrite_back(src, newer, old, new, journal)
            return
    if journal is not None:
        journal.moved(dst, src, files=[str(f.relative_to(dst)) for f in _files_in(dst)]
                      if dst.is_dir() and not dst.is_symlink() else None)
    if not case_only:
        src.parent.mkdir(parents=True, exist_ok=True)
    os.rename(dst, src)
    _rewrite_back(src, newer, old, new, journal)


def _rewrite_back(project_dir: Path, rels: List[str], old: str, new: str,
                  journal: Optional[Journal]) -> None:
    """Give session files that were written at the new path since, and now go
    back with their session, the old path too (as appended lines get)."""
    for rel in rels:
        rewrite_jsonl_field(project_dir / rel, "cwd", new, old,
                            before_write=journal.save_linked if journal else None)


def _revert_history(history: Path, before: Path, old: str, new: str,
                    journal: Optional[Journal]) -> None:
    """Revert exactly the history lines the migration changed; prompts added
    since keep the path they were typed in."""
    original = set()
    with open(before, "rb") as f:
        for line in f:
            if rewrite_line_field(line, "project", old, old) is not None:
                original.add(line.rstrip(b"\r\n"))
    if not original:
        return
    with file_lock(history, lock_path=os.path.realpath(history) + ".lock"):
        with open(history, "rb") as f:
            lines = f.readlines()
        out, changed = [], False
        for line in lines:
            reverted = rewrite_line_field(line, "project", new, old)
            if reverted is not None and reverted.rstrip(b"\r\n") in original:
                out.append(reverted)
                changed = True
            else:
                out.append(line)
        if changed:
            if journal is not None:
                journal.save_linked(history)
            with AtomicFile(history, keep_times=True) as f:
                f.writelines(out)


def _revert_claude_json(config: Path, before: Path, old: str, new: str,
                        journal: Optional[Journal]) -> None:
    """Move the project settings the migration moved back to their old keys,
    keeping changes made since and each key's position; an entry the new path
    had before the migration gets its old value back."""
    prior = json.loads(before.read_text(encoding="utf-8"))
    with file_lock(config, lock_path=str(config) + ".lock"):
        raw = config.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        changed = False
        projects, prior_projects = data.get("projects"), prior.get("projects")
        if isinstance(projects, dict) and isinstance(prior_projects, dict):
            back = {}  # key now -> key before the migration
            for key in prior_projects:
                moved_to = remap_path(key, old, new)
                if moved_to is not None and moved_to != key and key not in projects:
                    back[moved_to] = key
            if back:
                rebuilt: dict = {}
                for key, value in projects.items():
                    if key in back:
                        rebuilt[back[key]] = value
                        if key in prior_projects:
                            rebuilt[key] = prior_projects[key]
                    else:
                        rebuilt[key] = value
                for moved_to, key in back.items():
                    if moved_to not in projects:
                        rebuilt.setdefault(key, prior_projects[key])
                data["projects"] = rebuilt
                changed = True
        repos, prior_repos = data.get("githubRepoPaths"), prior.get("githubRepoPaths")
        if isinstance(repos, dict) and isinstance(prior_repos, dict):
            for repo, paths in repos.items():
                before_paths = prior_repos.get(repo)
                if not isinstance(paths, list) or not isinstance(before_paths, list):
                    continue
                out: list = []
                for p in paths:
                    back_to = remap_path(p, new, old) if isinstance(p, str) else None
                    if back_to is not None and back_to in before_paths and p not in before_paths:
                        p, changed = back_to, True
                    if p not in out:
                        out.append(p)
                repos[repo] = out
        if changed:
            if journal is not None:
                journal.save(config)
            atomic_write(config, json_bytes(data, indent=2) + (b"\n" if raw.endswith(b"\n") else b""))


def undo_blockers(backup_dir: Path, claude_dir: Path, claude_json: Path) -> List[str]:
    """Reasons a completed migration can't be undone cleanly right now.

    Checked before anything is changed, so an undo either runs to the end or
    does not start.
    """
    from claudepath.auto import _claude_processes_in

    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    old, new = manifest["old_path"], manifest["new_path"]
    blockers = []
    running = _claude_processes_in(new, old)
    if running:
        blockers.append(f"a Claude session (pid {running[0]}) is running in {shorten(new)}")
    if os.path.lexists(old):
        try:
            same = os.path.samefile(old, new)
        except OSError:
            same = False
        if not same:
            blockers.append(f"{shorten(old)} now holds another folder, which would see these sessions")
    for op in manifest["ops"]:
        if op["op"] != "move" or not os.path.lexists(op["dst"]):
            continue
        src, dst = Path(op["src"]), Path(op["dst"])
        src_project = _project_dir_of(src, claude_dir)
        dst_project = _project_dir_of(dst, claude_dir)
        if src_project is None or not src_project.exists():
            continue
        if dst_project is not None and dst_project.exists() and os.path.samefile(src_project, dst_project):
            files = op.get("files")
            if files is not None and any(
                _unit(str(f.relative_to(dst))) not in {_unit(r) for r in files} for f in _files_in(dst)
            ):
                blockers.append(
                    "only the letter case changed, and sessions were started since under the new "
                    "spelling; on this volume both spellings are one directory, so they can't be "
                    "told apart -- rename the folder back instead"
                )
        else:
            blockers.append(f"Claude Code was used at {shorten(old)} again since "
                            f"({src_project.name} exists)")
    saved = {op["path"]: backup_dir / op["backup"] for op in manifest["ops"] if op["op"] == "save"}
    if str(claude_json) in saved and claude_json.exists():
        prior = json.loads(saved[str(claude_json)].read_text(encoding="utf-8")).get("projects") or {}
        current = json.loads(claude_json.read_text(encoding="utf-8")).get("projects") or {}
        for key in prior:
            if remap_path(key, old, new) not in (None, key) and key in current:
                blockers.append(f"~/.claude.json has settings for {shorten(key)} again")
    return sorted(set(blockers))


def undo_migration(
    backup_dir: Path, claude_dir: Optional[Path] = None, claude_json: Optional[Path] = None
) -> Path:
    """Put a completed migration's data back under its old path.

    Keeps what Claude Code wrote after the migration: session files move back
    as whole sessions with their cwd rewritten in place (lines appended since
    stay), sessions started at the new path since stay there, and only the
    history lines and settings the migration changed are reverted. The undo is
    recorded so `claudepath auto` does not redo it, and it is journaled itself.

    Raises MigrationError, changing nothing, if undo_blockers() reports a
    reason; if it fails half way, its own changes are reverted. Callers hold
    registry_lock(). Returns the backup dir of the undo.
    """
    if claude_dir is None:
        claude_dir = find_claude_dir()
    if claude_json is None:
        claude_json = find_claude_json(claude_dir)
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "complete" or not manifest.get("old_path"):
        raise MigrationError(
            f"{backup_dir.name} is not a completed migration (status: {manifest.get('status')})"
        )
    blockers = undo_blockers(backup_dir, claude_dir, claude_json)
    if blockers:
        raise MigrationError("Can't undo cleanly: " + "; ".join(blockers) + ". Nothing was changed.")
    old, new = manifest["old_path"], manifest["new_path"]
    journal = Journal(get_journal_base(claude_dir), f"undo {backup_dir.name}",
                      {"old_path": new, "new_path": old}, recoverable=True)
    try:
        with _signals_raise():
            problems = _revert(manifest, backup_dir, claude_dir, claude_json, journal)
            if problems:
                raise MigrationError("; ".join(problems))
            reg = load_registry(claude_dir)
            decline(reg, old, new)
            save_registry(claude_dir, reg)
    except BaseException as e:
        with _signals_ignored():
            problems = _revert(journal.manifest, journal.dir, claude_dir, claude_json)
            journal.finish("rolled back" if not problems else "rollback failed", error=repr(e))
        if isinstance(e, _Terminated):
            raise SystemExit(128 + e.signum)
        if not isinstance(e, Exception):
            raise
        raise MigrationError(f"Undo failed: {e}\nIts changes were reverted.") from e
    journal.finish()
    manifest["status"] = "undone"
    manifest["undone_by"] = journal.dir.name
    atomic_write(manifest_path, json_bytes(manifest, indent=2) + b"\n")
    return journal.dir
