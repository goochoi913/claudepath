"""
Backup and rollback utilities for claudepath.

Before modifying any Claude Code data files, a backup is created.
If any step fails, the backup can be restored automatically.

`mv` and `remap` store backups in: ~/.claude/backups/claudepath/{timestamp}/

The automatic mode (`auto`, `scan`, `install`) uses a Journal instead, stored
in ~/.claude/claudepath/backups/{timestamp}/: every file is saved right before
it changes and every rename is logged, so the whole operation can be undone
step by step (see restore_journal).
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set


def create_backup(
    project_dir: Optional[Path],
    history_path: Path,
    backup_base: Path,
    extra_dir: Optional[Path] = None,
    old_path: Optional[str] = None,
) -> Path:
    """Create a backup of the project directory, history.jsonl, and usage-data.

    Args:
        project_dir: The ~/.claude/projects/{encoded}/ directory to back up.
        history_path: The ~/.claude/history.jsonl file to back up.
        backup_base: Base directory for backups (~/.claude/backups/claudepath/).
        extra_dir: Optional second project dir to back up (used during --merge).
        old_path: Original project path, used to identify usage-data files to back up.

    Returns:
        Path to the created backup directory.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_base / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Back up the source project directory
    if project_dir and project_dir.exists():
        dest = backup_dir / "project_dir"
        shutil.copytree(str(project_dir), str(dest))

    # Back up the merge target directory (destination that already has data)
    if extra_dir is not None and extra_dir.exists():
        dest = backup_dir / "merge_target_dir"
        shutil.copytree(str(extra_dir), str(dest))

    # Back up history.jsonl
    if history_path.exists():
        shutil.copy2(str(history_path), str(backup_dir / "history.jsonl"))

    # Back up usage-data session-meta files that match the old project path
    claude_dir = history_path.parent
    if old_path:
        _backup_usage_data(claude_dir, old_path, backup_dir)

    # Write a manifest so restore knows what to put back where
    manifest_lines = [
        f"project_dir={project_dir or ''}",
        f"history_path={history_path}",
    ]
    if extra_dir is not None:
        manifest_lines.append(f"merge_target_dir={extra_dir}")
    manifest = backup_dir / "manifest.txt"
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    return backup_dir


def _backup_usage_data(claude_dir: Path, old_path: str, backup_dir: Path) -> None:
    """Back up usage-data/session-meta files whose project_path matches old_path."""
    meta_dir = claude_dir / "usage-data" / "session-meta"
    if not meta_dir.exists():
        return

    backup_meta_dir = None
    for json_file in meta_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pp = data.get("project_path", "")
        if pp == old_path or pp.startswith(old_path + "/"):
            if backup_meta_dir is None:
                backup_meta_dir = backup_dir / "usage-data" / "session-meta"
                backup_meta_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(json_file), str(backup_meta_dir / json_file.name))


def restore_backup(backup_dir: Path) -> bool:
    """Restore files from a backup directory created by create_backup().

    Reads the manifest to know where to restore each item.
    Returns True on success, False if anything went wrong.
    """
    manifest = backup_dir / "manifest.txt"
    if not manifest.exists():
        return False

    config = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            config[k.strip()] = v.strip()

    project_dir = Path(config.get("project_dir", ""))
    history_path = Path(config.get("history_path", ""))

    success = True

    # Restore project directory (atomic: rename-aside, copy, cleanup)
    backup_project = backup_dir / "project_dir"
    if backup_project.exists() and project_dir:
        success = _atomic_restore_dir(backup_project, project_dir) and success

    # Restore merge target directory (if backed up during --merge)
    merge_target_dir = Path(config.get("merge_target_dir", ""))
    backup_merge_target = backup_dir / "merge_target_dir"
    if backup_merge_target.exists() and merge_target_dir and str(merge_target_dir) != ".":
        success = _atomic_restore_dir(backup_merge_target, merge_target_dir) and success

    # Restore history.jsonl
    backup_history = backup_dir / "history.jsonl"
    if backup_history.exists() and history_path:
        try:
            shutil.copy2(str(backup_history), str(history_path))
        except OSError:
            success = False

    # Restore usage-data session-meta files
    backup_meta = backup_dir / "usage-data" / "session-meta"
    if backup_meta.exists() and history_path:
        claude_dir = Path(config.get("history_path", "")).parent
        meta_dir = claude_dir / "usage-data" / "session-meta"
        if meta_dir.exists():
            for json_file in backup_meta.glob("*.json"):
                try:
                    shutil.copy2(str(json_file), str(meta_dir / json_file.name))
                except OSError:
                    success = False

    return success


def _atomic_restore_dir(backup_src: Path, target: Path) -> bool:
    """Atomically restore a directory from backup using rename-aside strategy.

    If the target exists, it's renamed aside first. If the copy fails, the
    original is renamed back, preventing data loss.
    """
    temp_old = None
    try:
        if target.exists():
            temp_old = target.with_name(target.name + ".claudepath-old")
            # Clean up any stale temp from a previous failed restore
            if temp_old.exists():
                shutil.rmtree(temp_old)
            os.rename(target, temp_old)
        shutil.copytree(str(backup_src), str(target))
        # Copy succeeded — clean up the old directory
        if temp_old and temp_old.exists():
            shutil.rmtree(temp_old)
        return True
    except OSError:
        # Restore the original directory if we renamed it aside
        if temp_old and temp_old.exists() and not target.exists():
            os.rename(temp_old, target)
        return False


def get_backup_base(claude_dir: Path) -> Path:
    """Return the base directory for claudepath backups."""
    return claude_dir / "backups" / "claudepath"


def find_latest_backup(backup_base: Path) -> Optional[Path]:
    """Return the most recently created backup directory, or None."""
    if not backup_base.exists():
        return None
    backups = sorted(
        [d for d in backup_base.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    return backups[0] if backups else None


def list_backups(backup_base: Path) -> List[Dict]:
    """List all backups with metadata parsed from their manifests.

    Returns a list of dicts sorted newest-first with keys:
        - timestamp: the directory name (e.g. "20260227_145300")
        - path: full Path to the backup directory
        - project_dir: original project directory that was backed up
        - has_merge_target: whether this backup includes a merge target
    """
    if not backup_base.exists():
        return []

    results = []
    for entry in sorted(backup_base.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest = entry / "manifest.txt"
        info: Dict = {
            "timestamp": entry.name,
            "path": entry,
            "project_dir": "",
            "has_merge_target": False,
        }
        if manifest.exists():
            try:
                for line in manifest.read_text(encoding="utf-8").splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k == "project_dir":
                            info["project_dir"] = v
                        elif k == "merge_target_dir":
                            info["has_merge_target"] = True
            except OSError:
                pass
        results.append(info)

    return results


# ─── Journal (automatic mode) ──────────────────────────────────────────────

JOURNAL_VERSION = 1

# A journal in one of these states has nothing left to undo.
UNDONE = {"restored", "rolled back", "rolled back (interrupted)", "undone"}
IN_PROGRESS = "in-progress"


def clear_marker(base: Path, name: str) -> None:
    """Remove the in-progress marker if it names this journal."""
    marker = base / IN_PROGRESS
    try:
        if marker.read_text(encoding="utf-8").strip() == name:
            marker.unlink()
    except OSError:
        pass


def get_journal_base(claude_dir: Path) -> Path:
    """Return the base directory for automatic-mode backups."""
    return claude_dir / "claudepath" / "backups"


def _new_backup_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for n in range(1, 1000):
        candidate = base / (stamp if n == 1 else f"{stamp}_{n}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"could not create a backup directory in {base}")


class Journal:
    """File backups plus an undo log for one automatic-mode operation.

    Layout of ~/.claude/claudepath/backups/{timestamp}/:
        manifest.json  every step, in order: files saved before they changed
                       ("save"), files removed ("delete") and renames ("move")
        files/         the saved files

    Files are copied, except through save_linked(): for files whose writer
    replaces them via a temp file + rename (the session and history rewriters),
    a hard link keeps the original bytes at no cost, so multi-gigabyte session
    histories are backed up instantly. Never link a file that is then
    rewritten in place -- the "backup" would change with it.
    """

    def __init__(self, base: Path, reason: str, details: Optional[Dict] = None,
                 recoverable: bool = False):
        from claudepath import __version__

        self.dir = _new_backup_dir(base)
        # A recoverable journal (a migration) leaves a marker while it runs, so
        # one that dies half way is found -- and reverted -- at the next launch.
        self.marker = base / IN_PROGRESS if recoverable else None
        self.files_dir = self.dir / "files"
        self.files_dir.mkdir()
        self._saved: Set[str] = set()
        self.manifest: Dict = {
            "version": JOURNAL_VERSION,
            "tool": f"claudepath {__version__}",
            "created": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "status": "in progress",
            "ops": [],
        }
        if details:
            self.manifest.update(details)
        self.flush()
        if self.marker is not None:
            from claudepath.fsutil import atomic_write

            atomic_write(self.marker, self.dir.name.encode())

    # Recording ------------------------------------------------------------

    def save(self, path: Path, link: bool = False) -> None:
        """Back up `path` before it is changed (once per path; no-op if missing)."""
        key = str(path)
        if key in self._saved or not path.is_file():
            return
        self._saved.add(key)
        self.manifest["ops"].append(
            {"op": "save", "path": key, "backup": self._store(path, link)}
        )
        self.flush()

    def save_linked(self, path: Path) -> None:
        """save() for a file about to be replaced by rename (see class doc)."""
        self.save(path, link=True)

    def deleted(self, path: Path) -> None:
        """Back up `path`, which the caller is about to delete."""
        self.manifest["ops"].append(
            {"op": "delete", "path": str(path), "backup": self._store(path, link=True)}
        )
        self._saved.add(str(path))
        self.flush()

    def moved(self, src: Path, dst: Path, files: Optional[List[str]] = None) -> None:
        """Log a rename the caller is about to make (file or directory).

        For a directory, `files` lists its contents (relative paths) at the
        time, so an undo can tell them from files that appeared later.
        """
        op: Dict = {"op": "move", "src": str(src), "dst": str(dst)}
        if files is not None:
            op["files"] = files
        self.manifest["ops"].append(op)
        self.flush()

    def made_dir(self, path: Path) -> None:
        """Log a directory the caller just created (removed again on undo)."""
        self.manifest["ops"].append({"op": "mkdir", "path": str(path)})
        self.flush()

    def created(self, path: Path) -> None:
        """Log a file the caller just created (deleted again on undo)."""
        self.manifest["ops"].append({"op": "create", "path": str(path)})
        self.flush()

    def finish(self, status: str = "complete", error: Optional[str] = None) -> None:
        self.manifest["status"] = status
        if error:
            self.manifest["error"] = error
        self.flush()
        if self.marker is not None:
            clear_marker(self.marker.parent, self.dir.name)

    @property
    def is_empty(self) -> bool:
        return not self.manifest["ops"]

    def discard(self) -> None:
        """Remove the backup directory (only sensible when nothing was recorded)."""
        shutil.rmtree(self.dir, ignore_errors=True)

    # Internals -------------------------------------------------------------

    def _store(self, path: Path, link: bool) -> str:
        n = len(self.manifest["ops"]) + 1
        rel = f"files/{n:06d}-{path.name}"
        dest = self.dir / rel
        if link:
            try:
                os.link(path, dest)
                return rel
            except OSError:
                pass
        shutil.copy2(str(path), str(dest))
        return rel

    def flush(self) -> None:
        from claudepath.fsutil import atomic_write

        data = json.dumps(self.manifest, indent=2, ensure_ascii=False) + "\n"
        atomic_write(self.dir / "manifest.json", data.encode("utf-8"))


def _locked_names() -> Set[str]:
    # Files Claude Code writes under a proper-lockfile lock.
    return {".claude.json", "history.jsonl"}


def _copy_back(backup_file: Path, target: Path) -> None:
    """Restore `target` from `backup_file` by copying (never linking, so the
    backup stays intact) through a temp file + rename, keeping its mtime."""
    from claudepath.fsutil import AtomicFile, file_lock

    target.parent.mkdir(parents=True, exist_ok=True)

    def copy() -> None:
        with AtomicFile(target) as out, open(backup_file, "rb") as src:
            shutil.copyfileobj(src, out, 1024 * 1024)
        st = os.stat(backup_file)
        os.chmod(target, st.st_mode & 0o7777)
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))

    if target.name in _locked_names():
        lock = os.path.realpath(target) + ".lock" if target.name == "history.jsonl" else None
        with file_lock(target, timeout=10.0, lock_path=lock):
            copy()
    else:
        copy()


def restore_journal(backup_dir: Path, final_status: str = "restored", force: bool = False) -> List[str]:
    """Undo a journaled operation exactly: walk its steps backwards, moving
    renamed paths back and copying the saved files into place.

    This is the right undo for a rollback (nothing happened since) and for
    small files like a shell rc file. To undo a migration that completed a
    while ago, use migrate.undo_migration(), which keeps what Claude Code wrote
    since -- this function refuses a completed migration unless `force` is
    set. A journal that was already rolled back, restored or undone is left
    alone.

    Returns a list of problems (empty on full success).
    """
    manifest_path = backup_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"cannot read {manifest_path}: {e}"]

    if manifest.get("status") in UNDONE:
        return []  # already undone; doing it again would clobber newer changes
    if manifest.get("status") == "complete" and manifest.get("old_path") and not force:
        return [f"{backup_dir.name} is a completed migration: undo it with "
                "migrate.undo_migration() (claudepath restore), which keeps newer data"]

    problems: List[str] = []
    for op in reversed(manifest.get("ops", [])):
        try:
            if op["op"] == "save" and not Path(op["path"]).parent.is_dir():
                problems.append(
                    f"not restoring {op['path']}: its directory no longer exists "
                    f"(saved copy: {backup_dir / op['backup']})"
                )
            elif op["op"] == "move":
                src, dst = Path(op["src"]), Path(op["dst"])
                if os.path.lexists(dst) and not os.path.lexists(src):
                    src.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(dst, src)
                elif os.path.lexists(dst) and os.path.samefile(src, dst):
                    if os.path.basename(os.path.realpath(dst)) != src.name:
                        os.rename(dst, src)  # undo a case-only rename
                elif os.path.lexists(dst):
                    problems.append(f"cannot move {dst} back: {src} exists")
            elif op["op"] in ("save", "delete"):
                _copy_back(backup_dir / op["backup"], Path(op["path"]))
            elif op["op"] == "mkdir":
                try:
                    os.rmdir(op["path"])
                except OSError:
                    pass  # gone already, or not empty: leave it
            elif op["op"] == "create":
                try:
                    os.unlink(op["path"])
                except FileNotFoundError:
                    pass
        except (OSError, KeyError) as e:
            problems.append(f"{op.get('op')} {op.get('path') or op.get('dst')}: {e}")

    manifest["status"] = final_status if not problems else f"{final_status}, with problems"
    manifest["restored"] = datetime.now().isoformat(timespec="seconds")
    try:
        from claudepath.fsutil import atomic_write

        atomic_write(
            manifest_path,
            (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
    except OSError:
        pass
    return problems


def list_journals(base: Path, limit: Optional[int] = None) -> List[Dict]:
    """List automatic-mode backups, newest first, with their manifest info."""
    if not base.exists():
        return []
    results = []
    for entry in sorted(base.iterdir(), reverse=True):
        if limit is not None and len(results) >= limit:
            break
        manifest = entry / "manifest.json"
        if not entry.is_dir() or not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        results.append(
            {
                "timestamp": entry.name,
                "path": entry,
                "reason": data.get("reason", ""),
                "status": data.get("status", ""),
                "old_path": data.get("old_path", ""),
                "new_path": data.get("new_path", ""),
                "ops": data.get("ops", []),
            }
        )
    return results
