"""
Scanner for Claude Code project data in ~/.claude/.
"""

import collections
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from claudepath.encoder import encode_path


def find_claude_dir() -> Path:
    """Return the Claude Code data directory.

    That is ~/.claude, or $CLAUDE_CONFIG_DIR when it is set -- Claude Code
    keeps projects/ and history.jsonl there in that case.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".claude"


def find_claude_json(claude_dir: Path) -> Path:
    """Return the path of Claude Code's global config (~/.claude.json).

    With $CLAUDE_CONFIG_DIR set, Claude Code keeps it inside that directory;
    otherwise it sits next to ~/.claude, in the home directory.
    """
    inside = claude_dir / ".claude.json"
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if (env and Path(env) == claude_dir) or inside.exists():
        return inside
    return claude_dir.parent / ".claude.json"


def read_first_cwd(jsonl_file: Path, max_lines: int = 500) -> Optional[str]:
    """Return the first top-level cwd recorded in a session file, or None.

    Faster than _read_cwd_from_jsonl on large sessions: lines are not parsed,
    the field is located directly (see updaters.find_top_level_string).
    """
    from claudepath.updaters import find_top_level_string

    try:
        with open(jsonl_file, "rb") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                if b'"cwd"' not in line:
                    continue
                found = find_top_level_string(line, "cwd")
                if found and isinstance(found[2], str) and found[2]:
                    return found[2]
    except OSError:
        pass
    return None


def project_paths_in_dir(project_dir: Path, limit: Optional[int] = None) -> List[str]:
    """Return the project path(s) a ~/.claude/projects/ directory belongs to.

    Reads sessions-index.json (older Claude Code versions) and the first cwd of
    each top-level session file, newest first; `limit` caps how many session
    files are read. More than one path means several folders share the slug.
    Most frequent first.
    """
    counts: "collections.Counter[str]" = collections.Counter()
    index_file = project_dir / "sessions-index.json"
    if index_file.exists():
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
            original = data.get("originalPath")
            if isinstance(original, str) and original:
                counts[original] += 1
            for e in data.get("entries", []):
                pp = e.get("projectPath")
                if isinstance(pp, str) and pp:
                    counts[pp] += 1
        except (json.JSONDecodeError, OSError, AttributeError):
            pass

    try:
        sessions = sorted(
            project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True
        )
    except OSError:
        sessions = []
    read = 0
    for f in sessions:
        if limit is not None and read >= limit:
            break
        cwd = read_first_cwd(f)
        if cwd:
            counts[cwd] += 1
            read += 1
    return [p for p, _ in counts.most_common()]


def find_project_dir(claude_dir: Path, project_path: str) -> Optional[Path]:
    """Find the encoded project directory in ~/.claude/projects/ for a given absolute path.

    Tries the computed encoded name first. Falls back to scanning sessions-index.json
    files, then to reading the cwd field from .jsonl files (handles cases where
    sessions-index.json is missing or corrupted).

    Returns the Path to the project dir, or None if not found.
    """
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        return None

    # Primary: match by computed encoding
    encoded = encode_path(project_path)
    candidate = projects_dir / encoded
    if candidate.exists():
        return candidate

    # Fallback: scan each project dir for any signal pointing at project_path.
    # First try sessions-index.json; if that's missing/invalid, probe a .jsonl cwd field.
    normalized = str(Path(project_path).resolve())
    for entry in projects_dir.iterdir():
        if not entry.is_dir():
            continue

        matched_via_index = False
        index_file = entry / "sessions-index.json"
        if index_file.exists():
            try:
                data = json.loads(index_file.read_text(encoding="utf-8"))
                matched_via_index = True
                original = data.get("originalPath", "")
                if original and str(Path(original).resolve()) == normalized:
                    return entry
                entries = data.get("entries", [])
                if entries:
                    pp = entries[0].get("projectPath", "")
                    if pp and str(Path(pp).resolve()) == normalized:
                        return entry
            except (json.JSONDecodeError, OSError):
                matched_via_index = False

        # If the index didn't resolve the path (missing, invalid, or null fields),
        # peek at any .jsonl file's cwd — it always carries the real project path.
        if not matched_via_index or _index_lacks_path(index_file):
            cwd = _read_first_cwd_in_dir(entry)
            if cwd and str(Path(cwd).resolve()) == normalized:
                return entry

    return None


def _index_lacks_path(index_file: Path) -> bool:
    """Return True if sessions-index.json has no usable originalPath/projectPath."""
    if not index_file.exists():
        return True
    try:
        data = json.loads(index_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    if data.get("originalPath"):
        return False
    entries = data.get("entries", [])
    return not (entries and entries[0].get("projectPath"))


def _read_first_cwd_in_dir(project_dir: Path) -> Optional[str]:
    """Return the cwd from the first .jsonl that has one, or None."""
    for jsonl_file in project_dir.glob("*.jsonl"):
        cwd = _read_cwd_from_jsonl(jsonl_file)
        if cwd:
            return cwd
    return None


def _decode_encoded_name(encoded_name: str) -> Optional[str]:
    """Try to recover the real absolute path from an encoded directory name
    by checking which path components actually exist on disk.

    Uses DFS with backtracking: each '-' in the name could be either a path
    separator (originally '/') or a hyphen in a directory name. We probe the
    filesystem to disambiguate.

    Returns the real path string if found, None if the project no longer exists.
    """
    # Strip leading '-' (encodes the leading '/')
    parts = encoded_name.lstrip("-").split("-")

    def dfs(current: Path, remaining: List[str]) -> Optional[Path]:
        if not remaining:
            return current
        for i in range(1, len(remaining) + 1):
            candidate = current / "-".join(remaining[:i])
            if candidate.is_dir():
                result = dfs(candidate, remaining[i:])
                if result is not None:
                    return result
        return None

    found = dfs(Path("/"), parts)
    return str(found) if found else None


def _read_cwd_from_jsonl(jsonl_file: Path) -> Optional[str]:
    """Read the cwd field from the first user/assistant message in a .jsonl file."""
    try:
        with open(jsonl_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    cwd = obj.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return None


def list_projects(claude_dir: Path) -> List[Dict]:
    """List all Claude Code projects with metadata.

    Returns a list of dicts with keys:
        - encoded_name: the directory name under ~/.claude/projects/
        - project_path: the original absolute project path (from sessions-index or best guess)
        - session_count: number of .jsonl session files
        - last_modified: ISO timestamp of most recently modified session file
    """
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        return []

    results = []
    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir():
            continue

        project_path = None
        last_modified = None
        session_count = 0

        # Try to read project path from sessions-index.json
        index_file = entry / "sessions-index.json"
        if index_file.exists():
            try:
                data = json.loads(index_file.read_text(encoding="utf-8"))
                project_path = data.get("originalPath") or None
                entries = data.get("entries", [])
                # Fallback: use projectPath from first entry if originalPath is null
                if not project_path and entries:
                    project_path = entries[0].get("projectPath") or None
                session_count = len(entries)
                if entries:
                    last_modified = max(
                        (e.get("modified", "") for e in entries), default=None
                    )
            except (json.JSONDecodeError, OSError):
                pass

        # Count jsonl files as fallback for session count
        jsonl_files = list(entry.glob("*.jsonl"))
        if session_count == 0:
            session_count = len(jsonl_files)
            if jsonl_files and last_modified is None:
                import datetime
                most_recent = max(jsonl_files, key=lambda f: f.stat().st_mtime)
                last_modified = datetime.datetime.fromtimestamp(
                    most_recent.stat().st_mtime
                ).isoformat()

        # Fallback: read cwd from the first line of any .jsonl — always has the real path
        if not project_path and jsonl_files:
            project_path = _read_cwd_from_jsonl(jsonl_files[0])

        # Fallback: probe the filesystem to decode the encoded directory name
        if not project_path:
            project_path = _decode_encoded_name(entry.name)

        # Last resort: encoded name with leading - replaced by /
        if not project_path:
            project_path = entry.name.replace("-", "/", 1)

        results.append(
            {
                "encoded_name": entry.name,
                "project_path": project_path,
                "session_count": session_count,
                "last_modified": last_modified,
            }
        )

    return results
