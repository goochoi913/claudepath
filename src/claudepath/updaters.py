"""
Updaters for Claude Code data files after a project move/remap.

All updaters support dry_run mode: they compute what would change but do not
write anything. They return a count of files/lines modified.
"""

import json
import mmap
import os
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generator, List, Optional, Tuple

from claudepath.encoder import encode_path
from claudepath.fsutil import AtomicFile, atomic_write, file_lock


@contextmanager
def preserve_mtime(file_path: Path) -> Generator[None, None, None]:
    """Preserve atime/mtime of `file_path` across a write.

    Captures times on entry (if the file exists) and restores them on exit. If
    the file did not exist on entry, nothing is restored — a freshly created
    file is left with its natural creation time. Restore failures are
    swallowed so mtime preservation can never break the underlying write.
    """
    try:
        st = os.stat(file_path)
        times_ns = (st.st_atime_ns, st.st_mtime_ns)
    except FileNotFoundError:
        times_ns = None
    try:
        yield
    finally:
        if times_ns is not None:
            try:
                os.utime(file_path, ns=times_ns)
            except OSError:
                pass


def update_usage_data(
    claude_dir: Path,
    old_path: str,
    new_path: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Update project_path in usage-data/session-meta/*.json files.

    Returns the number of files updated.
    """
    meta_dir = claude_dir / "usage-data" / "session-meta"
    if not meta_dir.exists():
        return 0

    files_updated = 0
    for json_file in meta_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        pp = data.get("project_path", "")
        if pp == old_path or pp.startswith(old_path + "/"):
            data["project_path"] = new_path + pp[len(old_path):]
            files_updated += 1
            if verbose:
                print(f"    {json_file.name}: updated project_path", file=sys.stderr)
            if not dry_run:
                with preserve_mtime(json_file):
                    atomic_write(json_file, json_bytes(data, indent=4))

    return files_updated


def update_sessions_index(
    index_path: Path,
    old_path: str,
    new_path: str,
    new_encoded_dir: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Update sessions-index.json with the new project path.

    Updates three types of references:
    - "originalPath": the root-level original project path
    - entries[*]["projectPath"]: project path in each session entry
    - entries[*]["fullPath"]: absolute path to the .jsonl file (contains encoded dir name)

    Returns 1 if the file was updated, 0 if it did not need updating.
    """
    if not index_path.exists():
        return 0

    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0

    changed = False

    if data.get("originalPath") == old_path:
        data["originalPath"] = new_path
        changed = True

    old_encoded = encode_path(old_path)
    for entry in data.get("entries", []):
        if entry.get("projectPath") == old_path:
            entry["projectPath"] = new_path
            changed = True
        # fullPath looks like: /Users/foo/.claude/projects/{encoded}/{sessionId}.jsonl
        full_path = entry.get("fullPath", "")
        if old_encoded in full_path:
            entry["fullPath"] = full_path.replace(old_encoded, new_encoded_dir, 1)
            changed = True

    if verbose and changed:
        fields = []
        if data.get("originalPath") == new_path:
            fields.append("originalPath")
        fields.append("projectPath")
        fields.append("fullPath")
        print(f"    {index_path.name}: updated {', '.join(fields)}", file=sys.stderr)

    if changed and not dry_run:
        with preserve_mtime(index_path):
            atomic_write(index_path, json_bytes(data, indent=2))

    return 1 if changed else 0


def update_jsonl_files(
    project_dir: Path,
    old_path: str,
    new_path: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> Tuple[int, int]:
    """Replace all occurrences of old_path with new_path in every .jsonl file
    inside project_dir (recursively, including subagent dirs).

    Processes files line-by-line to handle large sessions (>9MB).

    Returns (files_updated, total_lines_changed).
    """
    files_updated = 0
    total_lines_changed = 0

    for jsonl_file in project_dir.rglob("*.jsonl"):
        lines_changed = replace_in_file(jsonl_file, old_path, new_path, dry_run)
        if lines_changed > 0:
            files_updated += 1
            total_lines_changed += lines_changed
            if verbose:
                rel = jsonl_file.relative_to(project_dir)
                print(f"    {rel}: {lines_changed} line(s) changed", file=sys.stderr)

    return files_updated, total_lines_changed


def update_history(
    history_path: Path,
    old_path: str,
    new_path: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Replace old_path with new_path in ~/.claude/history.jsonl.

    The history file has lines like:
        {"display":"...","project":"/old/path","timestamp":...}

    Returns the number of lines changed.
    """
    if not history_path.exists():
        return 0
    count = replace_in_file(history_path, old_path, new_path, dry_run)
    if verbose and count:
        print(f"    history.jsonl: {count} line(s) changed", file=sys.stderr)
    return count


def merge_sessions_index(
    dst_index: Path,
    src_index: Path,
    old_path: str,
    new_path: str,
    new_encoded: str,
    dry_run: bool = False,
) -> int:
    """Merge entries from src sessions-index.json into dst sessions-index.json.

    Entries from src are updated (old_path → new_path) before being appended.
    Entries that already exist in dst (by sessionId) are skipped.

    Returns the number of entries merged from src into dst.
    """
    if not dst_index.exists() or not src_index.exists():
        return 0

    try:
        dst_data = json.loads(dst_index.read_text(encoding="utf-8"))
        src_data = json.loads(src_index.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0

    old_encoded = encode_path(old_path)
    existing_ids = {e.get("sessionId") for e in dst_data.get("entries", [])}

    merged = 0
    for entry in src_data.get("entries", []):
        if entry.get("sessionId") in existing_ids:
            print(
                f"  Warning: skipping duplicate session '{entry.get('sessionId')}'",
                file=sys.stderr,
            )
            continue
        # Update paths in the entry
        if entry.get("projectPath") == old_path:
            entry["projectPath"] = new_path
        full_path = entry.get("fullPath", "")
        if old_encoded in full_path:
            entry["fullPath"] = full_path.replace(old_encoded, new_encoded, 1)
        dst_data.setdefault("entries", []).append(entry)
        merged += 1

    # Ensure originalPath is updated
    if dst_data.get("originalPath") == old_path:
        dst_data["originalPath"] = new_path

    if merged > 0 and not dry_run:
        with preserve_mtime(dst_index):
            atomic_write(dst_index, json_bytes(dst_data, indent=2))

    return merged


def replace_path_values(obj, old: str, new: str) -> bool:
    """Recursively replace path values in a parsed JSON object.

    Only replaces string values that are exactly `old` or start with `old/`.
    Returns True if any replacement was made.
    """
    changed = False
    if isinstance(obj, dict):
        items = obj.keys()
    elif isinstance(obj, list):
        items = range(len(obj))
    else:
        return False
    for key in items:
        val = obj[key]
        if isinstance(val, str):
            if val == old or val.startswith(old + "/"):
                obj[key] = new + val[len(old):]
                changed = True
        elif isinstance(val, (dict, list)):
            if replace_path_values(val, old, new):
                changed = True
    return changed


def replace_in_file(file_path: Path, old: str, new: str, dry_run: bool) -> int:
    """Replace path references of `old` with `new` in a JSONL file, line by line.

    Each line is parsed as JSON for safe, targeted replacement of path values.
    Only string values that are exactly `old` or start with `old/` are replaced,
    preventing substring corruption (e.g., /Users/foo won't match /Users/foobar).

    Falls back to string replacement for lines that aren't valid JSON.

    Writes atomically via a temp file to avoid partial writes on error.
    Returns the number of lines that contained at least one replacement.
    """
    lines_changed = 0
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return 0

    new_lines = []
    for line in lines:
        if old not in line:
            new_lines.append(line)
            continue

        stripped = line.rstrip("\n\r")
        try:
            obj = json.loads(stripped)
            if replace_path_values(obj, old, new):
                new_lines.append(json.dumps(obj, ensure_ascii=False) + "\n")
                lines_changed += 1
            else:
                new_lines.append(line)
        except (json.JSONDecodeError, ValueError):
            # Fallback for non-JSON lines: use exact-or-prefix string replacement
            new_lines.append(line.replace(old, new))
            lines_changed += 1

    if lines_changed > 0 and not dry_run:
        # Write atomically: write to temp file in same dir, then rename.
        # Capture the original (a)mtime so the post-rename file keeps the
        # session's true last-activity timestamp — Claude Code's project list
        # sorts on it, so a fresh "now" mtime would reorder every session.
        try:
            st = os.stat(file_path)
            times_ns = (st.st_atime_ns, st.st_mtime_ns)
        except FileNotFoundError:
            times_ns = None
        dir_path = file_path.parent
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            os.replace(tmp_path, file_path)
        except OSError:
            os.unlink(tmp_path)
            raise
        if times_ns is not None:
            try:
                os.utime(file_path, ns=times_ns)
            except OSError:
                pass

    return lines_changed


# ─── Surgical rewrites (automatic mode) ────────────────────────────────────
#
# The functions below change one top-level field of a JSONL line and leave
# every other byte of the file exactly as it was. They never re-serialise a
# line: the value token is spliced in place. That keeps session files
# byte-identical apart from the field itself (including any bytes that are not
# valid UTF-8), and keeps the rest of the line's formatting intact.

# A complete JSON string token, quotes included.
_STRING_TOKEN = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"', re.S)
_STRUCTURAL = re.compile(rb'[\[\]{}"]')
_SPACES = (0x20, 0x09)  # bytes Claude Code never writes, but other tools might


class ConcurrentWriteError(OSError):
    """A file was modified other than by appending while it was being rewritten."""


def json_bytes(obj, **kwargs) -> bytes:
    """Serialise like JSON.stringify: UTF-8, non-ASCII kept as is.

    A lone surrogate (say, half of an emoji cut off by a truncation) cannot be
    encoded as UTF-8; it is written as its \\uXXXX escape instead, exactly as
    JavaScript does, rather than failing the whole write.
    """
    return json.dumps(obj, ensure_ascii=False, **kwargs).encode("utf-8", "backslashreplace")


def json_escape(s: str) -> bytes:
    """The bytes `s` occupies inside a JSON string written by JSON.stringify."""
    return json_bytes(s)[1:-1]


def remap_path(value: str, old: str, new: str) -> Optional[str]:
    """Map `value` from under `old` to under `new`; None if it isn't under `old`.

    Matches the exact path or a descendant ("old/..."), never a sibling that
    merely shares the prefix ("/a/foo" does not match "/a/foobar"). Values are
    compared in NFC, the form Claude Code stores paths in.
    """
    v = unicodedata.normalize("NFC", value)
    if v == old:
        return new
    if v.startswith(old + "/"):
        return new + v[len(old):]
    return None


def _balance(line: bytes, start: int, end: int) -> Optional[int]:
    """Closing minus opening brackets in line[start:end], ignoring brackets
    inside strings. `start` must lie between tokens. Returns None if a string
    is still open at `end` (e.g. a line truncated by a crash).

    One pass over the structural characters: linear even for malformed input.
    """
    balance = 0
    in_string = False
    for m in _STRUCTURAL.finditer(line, start, end):
        i = m.start()
        c = line[i]
        if c == 0x22:  # '"'
            if in_string:
                j = i - 1
                while line[j] == 0x5C:  # backslashes escaping this quote
                    j -= 1
                if (i - 1 - j) % 2:
                    continue
            in_string = not in_string
        elif not in_string:
            balance += 1 if c in (0x7D, 0x5D) else -1
    return None if in_string else balance


def find_top_level_string(line: bytes, key: str) -> Optional[Tuple[int, int, str]]:
    """Locate a top-level string field in one JSON line without parsing the line.

    Returns (start, end, value) where line[start:end] is the raw string token
    (quotes included), or None if the line has no such top-level field. Nested
    objects that happen to use the same key are ignored.

    Claude Code's session lines put large values (message, toolUseResult,
    attachment) before "cwd", so a full json.loads per line would dominate the
    cost of a migration. Instead the occurrences of the key token are checked
    from the end of the line: one must sit where a key can (after "{" or ","),
    be followed by ":" and a string, and the brackets after its value must
    close exactly one level -- the root object. The bracket balance is carried
    from one candidate to the next, so the whole search is a single linear
    pass, and a line with an unterminated string is never matched.
    """
    key_tok = b'"' + key.encode("utf-8") + b'"'
    n = len(line)
    end = n
    boundary, tail = n, 0  # balance of line[boundary:], measured from a token boundary
    while True:
        i = line.rfind(key_tok, 0, end)
        if i < 0:
            return None
        end = i
        j = i - 1
        while j >= 0 and line[j] in _SPACES:
            j -= 1
        if j < 0 or line[j] not in (0x7B, 0x2C):  # "{" or ","
            continue
        k = i + len(key_tok)
        while k < n and line[k] in _SPACES:
            k += 1
        if k >= n or line[k] != 0x3A:  # ":"
            continue
        k += 1
        while k < n and line[k] in _SPACES:
            k += 1
        if k >= n or line[k] != 0x22:  # value must be a string
            continue
        m = _STRING_TOKEN.match(line, k, boundary)
        if not m:
            continue
        segment = _balance(line, m.end(), boundary)
        if segment is None:
            return None  # a string runs past this point: malformed or truncated
        boundary, tail = m.end(), segment + tail
        if tail != 1:
            continue
        try:
            value = json.loads(line[k:m.end()])
        except ValueError:
            return None
        return k, m.end(), value


def rewrite_line_field(line: bytes, key: str, old: str, new: str) -> Optional[bytes]:
    """Return `line` with its top-level `key` path moved from `old` to `new`,
    or None if the line does not need to change."""
    if json_escape(old) not in line:
        return None
    found = find_top_level_string(line, key)
    if found is None:
        return None
    start, end, value = found
    if not isinstance(value, str):
        return None
    moved = remap_path(value, old, new)
    if moved is None:
        return None
    return line[:start] + json_bytes(moved) + line[end:]


def _file_contains(path: Path, needle: bytes) -> bool:
    with open(path, "rb") as f:
        try:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                return m.find(needle) != -1
        except ValueError:  # empty file
            return False


def _is_complete_json(data: bytes) -> bool:
    try:
        json.loads(data)
        return True
    except ValueError:
        return False


class _LineCopier:
    """Copies a file being appended to, rewriting one field per complete line.

    A read that ends mid-line (a concurrent append caught halfway) is held back
    until the rest of the line arrives: a fragment of JSON can look like a
    complete object and mislead find_top_level_string(). A last line without a
    newline counts as complete if it parses -- no proper prefix of a JSON
    object is itself a valid object.
    """

    def __init__(self, key: str, old: str, new: str):
        self.key, self.old, self.new = key, old, new
        self.pending = b""
        self.changed = 0
        self.written = 0

    def copy(self, src, out) -> None:
        """Copy from src's current position to its end."""
        while True:
            chunk = src.readline()
            if not chunk:
                return
            line = self.pending + chunk
            if not line.endswith(b"\n") and not _is_complete_json(line):
                self.pending = line  # incomplete: wait for the rest
                continue
            self.pending = b""
            new_line = rewrite_line_field(line, self.key, self.old, self.new)
            if new_line is not None:
                line = new_line
                self.changed += 1
            out.write(line)
            self.written += len(line)

    def flush(self, out) -> None:
        """Write a final line that never got completed, untouched."""
        out.write(self.pending)
        self.written += len(self.pending)
        self.pending = b""


def rewrite_jsonl_field(
    path: Path,
    key: str,
    old: str,
    new: str,
    dry_run: bool = False,
    before_write: Optional[Callable[[Path], None]] = None,
) -> int:
    """Rewrite the top-level `key` of every line in a JSONL file whose value is
    `old` or under it. Nothing else in the file changes, byte for byte.

    The file is replaced atomically and keeps its mode and atime/mtime (Claude
    Code orders --resume by session mtime). `before_write(path)` is called just
    before the replace, so the caller can back the original up.

    Claude Code appends to session files without locking. Lines appended while
    the file is being rewritten are carried over -- including one that lands
    after the final check, which reaches the old inode, still open here. A file
    that shrinks meanwhile raises ConcurrentWriteError and is left untouched.

    Returns the number of lines changed (or that would change, in dry_run).
    """
    try:
        st0 = os.stat(path)
    except FileNotFoundError:
        return 0
    if st0.st_size == 0 or not _file_contains(path, json_escape(old)):
        return 0

    if dry_run:
        with open(path, "rb") as f:
            return sum(1 for line in f if rewrite_line_field(line, key, old, new) is not None)

    copier = _LineCopier(key, old, new)
    with open(path, "rb") as src:
        atomic = AtomicFile(path)
        with atomic as out:
            copier.copy(src, out)
            if copier.changed == 0:
                atomic.discard()
            else:
                out.flush()
                os.fsync(out.fileno())  # the slow part, before the critical window
                if before_write is not None:
                    before_write(path)
                while True:  # take in anything appended meanwhile
                    copier.copy(src, out)
                    size = os.fstat(src.fileno()).st_size
                    if size == src.tell():
                        break
                    if size < src.tell():
                        raise ConcurrentWriteError(f"{path} shrank while it was being rewritten")
                copier.flush(out)
        if not copier.changed:
            return 0
        # An append between the last check and the rename went to the old
        # inode, which `src` still has open: carry it over too.
        with open(path, "ab") as out:
            copier.copy(src, out)
            copier.flush(out)
        st = os.fstat(src.fileno())
        if os.stat(path).st_size == copier.written:
            # Nobody wrote to the new file since: give it the session's own
            # last-activity time. (If somebody did, its mtime is the newer one.)
            try:
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                pass
    return copier.changed


def rewrite_history_projects(
    history_path: Path,
    old: str,
    new: str,
    dry_run: bool = False,
    before_write: Optional[Callable[[Path], None]] = None,
) -> int:
    """Move the "project" field of ~/.claude/history.jsonl entries from `old`
    to `new`, under the same lock Claude Code takes to append to the file.

    Returns the number of lines changed.
    """
    if not history_path.exists():
        return 0
    if dry_run:
        return rewrite_jsonl_field(history_path, "project", old, new, dry_run=True)
    # proper-lockfile resolves the path before appending ".lock".
    lock = os.path.realpath(history_path) + ".lock"
    with file_lock(history_path, lock_path=lock):
        return rewrite_jsonl_field(
            history_path, "project", old, new, before_write=before_write
        )


def _merge_old_wins(old_value, new_value):
    """Merge two ~/.claude.json project entries; on conflicts the old one wins."""
    if isinstance(old_value, dict) and isinstance(new_value, dict):
        merged = dict(new_value)
        merged.update(old_value)
        return merged
    return old_value


def update_claude_json(
    config_path: Path,
    old: str,
    new: str,
    dry_run: bool = False,
    before_write: Optional[Callable[[Path], None]] = None,
) -> Tuple[List[Tuple[str, str]], int]:
    """Move per-project settings in ~/.claude.json from `old` to `new`.

    Re-keys the "projects" map (trust dialog, allowed tools, MCP servers, ...)
    for `old` and every subfolder of it, keeping key order. If the new key
    already exists the entries are merged and the old values win. Also fixes
    local checkout paths listed in "githubRepoPaths".

    The file is rewritten atomically, keeps its mode, and is read and written
    under the lock Claude Code uses, so a concurrent save cannot interleave.

    Returns (moved_keys, githubRepoPaths entries changed).
    """
    if not config_path.exists():
        return [], 0

    def apply() -> Tuple[List[Tuple[str, str]], int]:
        raw = config_path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return [], 0

        moved: List[Tuple[str, str]] = []
        projects = data.get("projects")
        if isinstance(projects, dict):
            result: dict = {}
            for key, value in projects.items():
                target = remap_path(key, old, new) if isinstance(key, str) else None
                if target is None:
                    # An untouched key may be where a moved key already landed.
                    result[key] = _merge_old_wins(result[key], value) if key in result else value
                else:
                    moved.append((key, target))
                    result[target] = (
                        _merge_old_wins(value, result[target]) if target in result else value
                    )
            if moved:
                data["projects"] = result

        repos_changed = 0
        repo_paths = data.get("githubRepoPaths")
        if isinstance(repo_paths, dict):
            for repo, paths in repo_paths.items():
                if not isinstance(paths, list):
                    continue
                out: list = []
                touched = False
                for p in paths:
                    target = remap_path(p, old, new) if isinstance(p, str) else None
                    if target is not None:
                        p, touched = target, True
                    if p not in out:
                        out.append(p)
                if touched:
                    repo_paths[repo] = out
                    repos_changed += 1

        if (moved or repos_changed) and not dry_run:
            if before_write is not None:
                before_write(config_path)
            out = json_bytes(data, indent=2)
            if raw.endswith(b"\n"):
                out += b"\n"
            atomic_write(config_path, out)
        return moved, repos_changed

    if dry_run:
        return apply()
    with file_lock(config_path, lock_path=str(config_path) + ".lock"):
        return apply()
