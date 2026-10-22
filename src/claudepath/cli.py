"""
CLI entry point for claudepath.
"""

import os
import subprocess
import sys
import threading
import json as _json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from claudepath import __version__
from claudepath.mover import MoveError, move_project, preview_operation, remap_project
from claudepath.scanner import find_claude_dir, find_claude_json, list_projects
from claudepath.backup import (
    UNDONE,
    find_latest_backup,
    get_backup_base,
    get_journal_base,
    list_backups,
    list_journals,
    restore_backup,
    restore_journal,
)

# ANSI color codes (no external dependencies)
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"

ALL_COMMANDS = [
    "mv", "remap", "list", "update", "restore", "help",
    "auto", "sync", "install", "uninstall", "scan", "status",
]


def supports_color() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(text: str, *codes: str) -> str:
    if not supports_color():
        return text
    return "".join(codes) + text + RESET


# ─── Help ──────────────────────────────────────────────────────────────────

def print_help() -> None:
    print(
        f"""
{_c("claudepath", BOLD, CYAN)} {_c(f"v{__version__}", DIM)} — Move Claude Code projects without losing session history

{_c("USAGE", BOLD)}
  claudepath <command> [options]

{_c("COMMANDS", BOLD)}
  {_c("mv", BOLD)} <old-path> <new-path>      Move project directory and update all Claude references
  {_c("remap", BOLD)} <old-path> <new-path>   Update Claude references only (directory already moved)
  {_c("list", BOLD)}                          List all projects tracked by Claude Code
  {_c("update", BOLD)}                        Update claudepath to the latest version
  {_c("restore", BOLD)}                       Restore from a previous backup
  {_c("help", BOLD)}                          Show this help message

{_c("AUTOMATIC MODE", BOLD)} — sessions follow folders you move or rename in Finder or a shell
  {_c("install", BOLD)}                       Run 'claudepath auto' before every 'claude' (shell rc block)
  {_c("uninstall", BOLD)}                     Remove the shell rc block
  {_c("sync", BOLD)}                          Record the identity of every existing project
  {_c("auto", BOLD)}                          Migrate the current folder's sessions if it moved (pre-launch hook)
  {_c("scan", BOLD)} <root>                   Rescue sessions whose folder already moved somewhere under <root>
  {_c("status", BOLD)}                        Tracked projects, orphans, hook and slug-encoding check

{_c("OPTIONS (mv / remap)", BOLD)}
  --dry-run        Preview changes without modifying any files
  --no-backup      Skip creating a backup before modifying files
  --yes            Skip interactive confirmation prompt
  --merge          Merge sessions when destination already has Claude data
  --verbose, -v    Show detailed output of each file processed
  --claude-dir     Override the Claude data directory (default: ~/.claude)

{_c("EXAMPLES", BOLD)}
  # Move a project to a new location
  claudepath mv ~/projects/old-name ~/projects/new-name

  # You already moved the directory manually — just update Claude's references
  claudepath remap ~/old/path ~/new/path

  # Preview what would change without touching anything
  claudepath mv ~/projects/old ~/projects/new --dry-run

  # List all Claude Code projects
  claudepath list

  # Restore from the latest backup
  claudepath restore

{_c("WHAT IT UPDATES", BOLD)}
  - ~/.claude/projects/{{encoded-dir}}/     (renamed)
  - ~/.claude/projects/.../sessions-index.json
  - ~/.claude/projects/.../{{session}}.jsonl  (all sessions, recursively)
  - ~/.claude/history.jsonl
  - ~/.claude/usage-data/session-meta/*.json
  - ~/.claude.json projects map and githubRepoPaths  (automatic mode)

{_c("BACKUP", BOLD)}
  By default, a backup is created before any changes in:
  ~/.claude/backups/claudepath/{{timestamp}}/   (mv, remap)
  ~/.claude/claudepath/backups/{{timestamp}}/   (auto, scan, install)
  Use --no-backup to skip (only if you have your own backup).

{_c("REPORT ISSUES", BOLD)}
  https://github.com/Mahiler1909/claudepath/issues
"""
    )


def _print_help_mv() -> None:
    print(f"""\
{_c("claudepath mv", BOLD)} — Move project directory and update Claude references

{_c("USAGE", BOLD)}
  claudepath mv <old-path> <new-path> [options]

{_c("OPTIONS", BOLD)}
  --dry-run        Preview changes without modifying any files
  --no-backup      Skip creating a backup before modifying files
  --yes, -y        Skip interactive confirmation prompt
  --merge          Merge sessions when destination already has Claude data
  --verbose, -v    Show detailed output of each file processed
  --claude-dir     Override the Claude data directory (default: ~/.claude)

{_c("EXAMPLES", BOLD)}
  claudepath mv ~/projects/old-name ~/projects/new-name
  claudepath mv ~/projects/old ~/projects/new --dry-run
  claudepath mv ~/old ~/new --merge --yes
""")


def _print_help_remap() -> None:
    print(f"""\
{_c("claudepath remap", BOLD)} — Update Claude references only (directory already moved)

{_c("USAGE", BOLD)}
  claudepath remap <old-path> <new-path> [options]

{_c("OPTIONS", BOLD)}
  --dry-run        Preview changes without modifying any files
  --no-backup      Skip creating a backup before modifying files
  --yes, -y        Skip interactive confirmation prompt
  --merge          Merge sessions when destination already has Claude data
  --verbose, -v    Show detailed output of each file processed
  --claude-dir     Override the Claude data directory (default: ~/.claude)

{_c("EXAMPLES", BOLD)}
  claudepath remap ~/old/path ~/new/path
  claudepath remap ~/old ~/new --merge
""")


def _print_help_list() -> None:
    print(f"""\
{_c("claudepath list", BOLD)} — List all projects tracked by Claude Code

{_c("USAGE", BOLD)}
  claudepath list [options]

{_c("OPTIONS", BOLD)}
  --claude-dir     Override the Claude data directory (default: ~/.claude)
""")


def _print_help_update() -> None:
    print(f"""\
{_c("claudepath update", BOLD)} — Update claudepath to the latest version

{_c("USAGE", BOLD)}
  claudepath update [options]

{_c("OPTIONS", BOLD)}
  --brew           Force update via Homebrew
  --pipx           Force update via pipx
  --pip            Force update via pip

Auto-detects the install method if no override is given.
""")


def _print_help_restore() -> None:
    print(f"""\
{_c("claudepath restore", BOLD)} — Restore from a previous backup

{_c("USAGE", BOLD)}
  claudepath restore [<timestamp>] [options]

{_c("OPTIONS", BOLD)}
  --list           List all available backups
  --dry-run        Show what would be restored without changing anything
  --claude-dir     Override the Claude data directory (default: ~/.claude)

{_c("EXAMPLES", BOLD)}
  claudepath restore              # restore latest backup
  claudepath restore --list       # list all backups
  claudepath restore 20260227_145300  # restore a specific backup
""")


def _print_help_sync() -> None:
    print(f"""\
{_c("claudepath sync", BOLD)} — Record the identity of every existing project

Stores device+inode, birth time, git root commit and remote for each project
folder Claude Code knows, in ~/.claude/claudepath/registry.json, so that
'claudepath auto' can recognise the folder after it is moved or renamed.
'claudepath install' runs this once; 'auto' keeps it current afterwards.

{_c("USAGE", BOLD)}
  claudepath sync [options]

{_c("OPTIONS", BOLD)}
  --dry-run        Show what would be recorded without writing the registry
  --verbose, -v    List every project
  --claude-dir     Override the Claude data directory (default: ~/.claude)
""")


def _print_help_install() -> None:
    print(f"""\
{_c("claudepath install", BOLD)} — Run 'claudepath auto' before every 'claude' launch

Appends a marked block to ~/.zshrc (or ~/.bashrc) after asking, then records
existing projects ('claudepath sync'). An existing claude function or alias is
kept and still runs. Undo with 'claudepath uninstall'.

{_c("USAGE", BOLD)}
  claudepath install [options]

{_c("OPTIONS", BOLD)}
  --shell <zsh|bash>  Shell to install for (default: from $SHELL)
  --rc <file>         Startup file to edit (default: ~/.zshrc or ~/.bashrc)
  --dry-run           Show the block without writing anything
  --yes, -y           Skip the confirmation prompt
  --claude-dir        Override the Claude data directory (default: ~/.claude)
""")


def _print_help_uninstall() -> None:
    print(f"""\
{_c("claudepath uninstall", BOLD)} — Remove the block added by 'claudepath install'

The registry and backups in ~/.claude/claudepath/ are kept.

{_c("USAGE", BOLD)}
  claudepath uninstall [options]

{_c("OPTIONS", BOLD)}
  --shell <zsh|bash>  Shell to uninstall for (default: from $SHELL)
  --rc <file>         Startup file to edit (default: ~/.zshrc or ~/.bashrc)
  --dry-run           Show what would be removed without writing anything
  --yes, -y           Skip the confirmation prompt
""")


def _print_help_scan() -> None:
    print(f"""\
{_c("claudepath scan", BOLD)} — Rescue sessions whose folder was moved before claudepath knew

Finds orphaned projects (their folder no longer exists) and looks for the
folder under <root>: first by the inode recorded in the registry (moved
without asking), then by a unique folder with the same name (asks first).

{_c("USAGE", BOLD)}
  claudepath scan <root> [options]

{_c("OPTIONS", BOLD)}
  --dry-run        Show matches without moving anything
  --yes, -y        Also accept same-name matches without asking
  --verbose, -v    Show per-directory details while migrating
  --claude-dir     Override the Claude data directory (default: ~/.claude)

{_c("EXAMPLES", BOLD)}
  claudepath scan ~ --dry-run
  claudepath scan ~/Documents/Projects
""")


def _print_help_status() -> None:
    print(f"""\
{_c("claudepath status", BOLD)} — Show what the automatic mode sees

Lists tracked projects and orphaned sessions, checks the shell hook, and
verifies that this Claude Code version's directory naming (slug encoding)
matches the real names in ~/.claude/projects.

{_c("USAGE", BOLD)}
  claudepath status [options]

{_c("OPTIONS", BOLD)}
  --rc <file>      Startup file to check for the hook (default: from $SHELL)
  --claude-dir     Override the Claude data directory (default: ~/.claude)
""")


_HELP_MAP = {
    "mv": _print_help_mv,
    "remap": _print_help_remap,
    "list": _print_help_list,
    "update": _print_help_update,
    "restore": _print_help_restore,
    "sync": _print_help_sync,
    "install": _print_help_install,
    "uninstall": _print_help_uninstall,
    "scan": _print_help_scan,
    "status": _print_help_status,
}


# ─── Utilities ─────────────────────────────────────────────────────────────

def confirm(prompt: str) -> bool:
    """Ask for user confirmation. Returns True if user confirms."""
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def print_error(msg: str) -> None:
    print(_c(f"Error: {msg}", RED), file=sys.stderr)


def _check_claude_running(project_path: str) -> bool:
    """Check if Claude Code might be using this project."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"claude.*{project_path}"],
            capture_output=True, timeout=2,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ─── Argument Parsing ──────────────────────────────────────────────────────

def parse_mv_remap_args(args: list, cmd_name: str = "mv") -> dict:
    """Parse arguments for mv and remap subcommands."""
    if "--help" in args or "-h" in args:
        fn = _HELP_MAP.get(cmd_name)
        if fn:
            fn()
        sys.exit(0)

    opts = {
        "old_path": None,
        "new_path": None,
        "dry_run": False,
        "no_backup": False,
        "yes": False,
        "merge": False,
        "verbose": False,
        "claude_dir": None,
    }

    positional = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--dry-run":
            opts["dry_run"] = True
        elif arg == "--no-backup":
            opts["no_backup"] = True
        elif arg in ("--yes", "-y"):
            opts["yes"] = True
        elif arg == "--merge":
            opts["merge"] = True
        elif arg in ("--verbose", "-v"):
            opts["verbose"] = True
        elif arg == "--claude-dir":
            i += 1
            if i >= len(args):
                print_error("--claude-dir requires a value")
                sys.exit(1)
            opts["claude_dir"] = args[i]
        elif arg.startswith("--"):
            print_error(f"Unknown option: {arg}")
            sys.exit(1)
        else:
            positional.append(arg)
        i += 1

    if len(positional) < 2:
        print_error("Both <old-path> and <new-path> are required.")
        print(f"\n  Usage: claudepath {cmd_name} <old-path> <new-path> [options]", file=sys.stderr)
        print(f"  Run 'claudepath {cmd_name} --help' for details.", file=sys.stderr)
        sys.exit(1)

    opts["old_path"] = positional[0]
    opts["new_path"] = positional[1]
    return opts


# ─── Commands ──────────────────────────────────────────────────────────────

def _run_operation(args: list, cmd_name: str, confirm_prompt: str, from_label: str, operation: Callable) -> None:
    opts = parse_mv_remap_args(args, cmd_name=cmd_name)
    old_path = str(Path(opts["old_path"]).expanduser().resolve())
    new_path = str(Path(opts["new_path"]).expanduser().resolve())
    claude_dir = Path(opts["claude_dir"]).expanduser() if opts["claude_dir"] else None
    dry_run = opts["dry_run"]
    no_backup = opts["no_backup"]
    merge = opts["merge"]
    verbose = opts["verbose"]

    if dry_run:
        print(_c("DRY RUN — no files will be modified", YELLOW, BOLD))
        print()

    print(f"  {_c(from_label, BOLD)} {old_path}")
    print(f"  {_c('To:  ', BOLD)} {new_path}")
    print()

    # Show preview of what will change
    if not dry_run and not opts["yes"]:
        preview = preview_operation(old_path, claude_dir=claude_dir)
        if preview["project_found"]:
            print(f"  {_c('Will update:', DIM)}")
            print(f"    - Project directory (rename)")
            if preview["session_count"]:
                print(f"    - {preview['session_count']} session file(s)")
            if preview["has_history"]:
                print(f"    - history.jsonl")
            if not no_backup:
                print(f"    - Backup will be created")
        else:
            print(f"  {_c('Warning:', YELLOW)} Project not found in Claude data.")
            print(f"  Tip: run 'claudepath list' to see tracked projects.")
        print()

        # Check if Claude Code is running
        if _check_claude_running(old_path):
            print(
                _c("  Warning: Claude Code may be using this project.", YELLOW, BOLD)
            )
            print("  Close it first to avoid data corruption.\n")

        if not confirm(confirm_prompt):
            print("Aborted.")
            sys.exit(0)

    try:
        result = operation(
            old_path, new_path, claude_dir=claude_dir, dry_run=dry_run,
            no_backup=no_backup, merge=merge, verbose=verbose,
        )
        print(_c("Done!", GREEN, BOLD))
        print(result.summary())
    except MoveError as e:
        print_error(str(e))
        sys.exit(1)


def cmd_mv(args: list) -> None:
    _run_operation(
        args,
        cmd_name="mv",
        confirm_prompt="Move project and update all Claude Code references?",
        from_label="From:",
        operation=move_project,
    )


def cmd_remap(args: list) -> None:
    _run_operation(
        args,
        cmd_name="remap",
        confirm_prompt="Update all Claude Code references to the new path?",
        from_label="Old: ",
        operation=remap_project,
    )


def cmd_list(args: list) -> None:
    if "--help" in args or "-h" in args:
        _print_help_list()
        return

    claude_dir = find_claude_dir()
    for arg in args:
        if arg == "--claude-dir" and args:
            idx = args.index("--claude-dir")
            if idx + 1 < len(args):
                claude_dir = Path(args[idx + 1]).expanduser()

    projects = list_projects(claude_dir)
    if not projects:
        print("No Claude Code projects found.")
        return

    print(_c(f"Claude Code projects in {claude_dir}/projects/\n", BOLD))
    for p in projects:
        path = p["project_path"]
        sessions = p["session_count"]
        modified = p["last_modified"] or "unknown"
        # Trim the modified timestamp to date+time for readability
        if "T" in modified:
            modified = modified[:16].replace("T", " ")

        exists = Path(path).exists() if path else False
        status = _c(" ✓", GREEN) if exists else _c(" ✗ orphaned", RED, DIM)
        print(f"  {_c(path, BOLD)}{status}")
        print(f"    {_c('sessions:', DIM)} {sessions}  {_c('last active:', DIM)} {modified}")
        print()

    total = len(projects)
    on_disk = sum(1 for p in projects if Path(p["project_path"]).exists())
    orphaned = total - on_disk
    parts = [f"{on_disk} on disk"]
    if orphaned:
        parts.append(f"{orphaned} orphaned")
    label = f"{total} project{'s' if total != 1 else ''}"
    print(_c(f"{label} ({', '.join(parts)})", DIM))


def cmd_restore(args: list) -> None:
    if "--help" in args or "-h" in args:
        _print_help_restore()
        return

    claude_dir = find_claude_dir()
    show_list = False
    dry_run = False
    timestamp = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--list":
            show_list = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--claude-dir":
            i += 1
            if i < len(args):
                claude_dir = Path(args[i]).expanduser()
        elif not arg.startswith("--"):
            timestamp = arg
        i += 1

    backup_base = get_backup_base(claude_dir)
    journal_base = get_journal_base(claude_dir)

    if show_list:
        journals = list_journals(journal_base)
        backups = list_backups(backup_base)
        if not backups and not journals:
            print("No backups found.")
            return
        if journals:
            print(_c(f"Automatic-mode backups in {journal_base}/\n", BOLD))
            for j in journals:
                print(f"  {_c(j['timestamp'], BOLD)}  {_c(j['reason'], DIM)}  {_c(j['status'], DIM)}")
                if j["old_path"]:
                    print(f"    {j['old_path']} → {j['new_path']}")
                print()
        if backups:
            print(_c(f"Available backups in {backup_base}/\n", BOLD))
        for b in backups:
            merge_tag = _c(" (merge)", DIM) if b["has_merge_target"] else ""
            print(f"  {_c(b['timestamp'], BOLD)}{merge_tag}")
            if b["project_dir"]:
                print(f"    {_c('project:', DIM)} {b['project_dir']}")
            print()
        return

    # Find the backup to restore: automatic-mode journals and mv/remap backups
    # share the timestamp naming, so the newest of either kind is "latest".
    journal_dir = None
    if timestamp:
        if (journal_base / timestamp / "manifest.json").exists():
            journal_dir = journal_base / timestamp
        backup_dir = backup_base / timestamp
        if journal_dir is None and not backup_dir.exists():
            print_error(f"Backup not found: {timestamp}")
            print("Run 'claudepath restore --list' to see available backups.", file=sys.stderr)
            sys.exit(1)
    else:
        backup_dir = find_latest_backup(backup_base)
        # Restorable automatic-mode backups; an undo is undone only on request.
        journals = [j for j in list_journals(journal_base) if j["status"] not in UNDONE
                    and not j["status"].startswith("undone") and not j["reason"].startswith("undo")]
        if journals and (not backup_dir or journals[0]["timestamp"] > backup_dir.name):
            journal_dir = journals[0]["path"]
        if not backup_dir and journal_dir is None and list_journals(journal_base, limit=1):
            print("  Nothing to restore: the automatic-mode backups were all undone or rolled back.")
            print("  See 'claudepath restore --list'.")
            return
        if not backup_dir and journal_dir is None:
            print_error("No backups found.")
            print("Backups are created automatically when running 'mv' or 'remap'.", file=sys.stderr)
            sys.exit(1)

    if journal_dir is not None:
        _restore_journal_interactive(journal_dir, claude_dir, dry_run)
        return

    print(f"  {_c('Backup:', BOLD)} {backup_dir.name}")

    # Show manifest info
    manifest = backup_dir / "manifest.txt"
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                print(f"    {_c(k.strip() + ':', DIM)} {v.strip()}")
    print()

    if dry_run:
        print(_c("DRY RUN — nothing was restored", YELLOW, BOLD))
        return
    if not confirm("Restore from this backup?"):
        print("Aborted.")
        return

    success = restore_backup(backup_dir)
    if success:
        print(_c("Restored successfully!", GREEN, BOLD))
    else:
        print_error("Restore completed with errors. Some files may not have been restored.")
        sys.exit(1)


def _restore_journal_interactive(journal_dir: Path, claude_dir: Path, dry_run: bool = False) -> None:
    from claudepath import registry as reg_mod
    from claudepath.migrate import MigrationError, revert_interrupted, undo_blockers, undo_migration

    manifest = _json.loads((journal_dir / "manifest.json").read_text(encoding="utf-8"))
    status = manifest.get("status", "")
    ops = manifest.get("ops", [])
    print(f"  {_c('Backup:', BOLD)} {journal_dir.name}  {_c(manifest.get('reason', ''), DIM)}  "
          f"{_c(status, DIM)}")
    if manifest.get("old_path"):
        print(f"    {manifest['old_path']} → {manifest.get('new_path', '')}")
    print()
    if status in UNDONE or status.startswith("undone"):
        print("  Nothing to restore: this change was already undone.")
        return

    if manifest.get("old_path") and status in ("in progress", "rollback failed"):
        print("  This migration was interrupted. Reverting it puts the data back under")
        print(f"  {manifest['old_path']}, keeping anything written since.")
        print()
        if dry_run:
            print(_c("DRY RUN — nothing was restored", YELLOW, BOLD))
            return
        if not confirm("Revert it?"):
            print("Aborted.")
            return
        with reg_mod.registry_lock(claude_dir, timeout=10.0):
            revert_interrupted(journal_dir, claude_dir)
        print(_c("Reverted.", GREEN, BOLD))
        return

    if status == "complete" and manifest.get("old_path"):
        # A finished migration: move the data back, keeping what was written since.
        blockers = undo_blockers(journal_dir, claude_dir, find_claude_json(claude_dir))
        if blockers:
            print_error("This migration can't be undone cleanly right now:")
            for blocker in blockers:
                print(f"  - {blocker}", file=sys.stderr)
            print("Nothing was changed.", file=sys.stderr)
            sys.exit(1)
        print(f"  Will move the Claude Code data back to {manifest['old_path']}.")
        print("  Session lines, prompts and setting changes made since are kept, sessions")
        print("  started at the new path since stay there, and 'claudepath auto' won't")
        print("  move this folder's data again automatically.")
        print()
        if dry_run:
            print(_c("DRY RUN — nothing was restored", YELLOW, BOLD))
            return
        if not confirm("Undo this migration?"):
            print("Aborted.")
            return
        try:
            with reg_mod.registry_lock(claude_dir, timeout=10.0):
                undo_dir = undo_migration(journal_dir, claude_dir)
        except MigrationError as e:
            print_error(str(e))
            sys.exit(1)
        print(_c("Undone.", GREEN, BOLD), _c(f"(this undo is itself backed up: {undo_dir.name})", DIM))
        return

    # Anything else (a shell rc change, or a migration that was interrupted):
    # put the saved copies back exactly.
    files = [o for o in ops if o["op"] in ("save", "delete")]
    moves = [o for o in ops if o["op"] == "move"]
    print(f"    {_c('undo:', DIM)} {len(moves)} rename(s), {len(files)} file(s) put back as saved")
    for o in files:
        print(f"    {_c('note:', YELLOW)} {o['path']} returns to its state at backup time; "
              "later changes to it are lost")
    print()
    if dry_run:
        for o in reversed(ops):
            if o["op"] == "move":
                print(f"    would move {o['dst']} back to {o['src']}")
            elif o["op"] in ("save", "delete"):
                print(f"    would restore {o['path']}")
        print(_c("DRY RUN — nothing was restored", YELLOW, BOLD))
        return
    if not confirm("Restore from this backup?"):
        print("Aborted.")
        return
    problems = restore_journal(journal_dir)
    if problems:
        print_error("Restore completed with errors:")
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)
    print(_c("Restored successfully!", GREEN, BOLD))


# ─── Automatic mode ────────────────────────────────────────────────────────

def _parse_opts(
    args: list,
    cmd_name: str,
    flags: Tuple[str, ...],
    valued: Tuple[str, ...] = (),
    max_positional: int = 0,
) -> Tuple[Dict, List[str]]:
    """Parse options for the automatic-mode commands (same conventions as mv/remap)."""
    if "--help" in args or "-h" in args:
        _HELP_MAP[cmd_name]()
        sys.exit(0)
    aliases = {"-y": "--yes", "-v": "--verbose"}
    opts: Dict = {}
    positional: List[str] = []
    i = 0
    while i < len(args):
        arg = aliases.get(args[i], args[i])
        key = arg.lstrip("-").replace("-", "_")
        if arg in flags:
            opts[key] = True
        elif arg in valued:
            i += 1
            if i >= len(args):
                print_error(f"{arg} requires a value")
                sys.exit(1)
            opts[key] = args[i]
        elif arg.startswith("-"):
            print_error(f"Unknown option: {arg}")
            print(f"Run 'claudepath {cmd_name} --help' for details.", file=sys.stderr)
            sys.exit(1)
        else:
            positional.append(arg)
        i += 1
    if len(positional) > max_positional:
        print_error(f"Unexpected argument: {positional[max_positional]}")
        sys.exit(1)
    return opts, positional


def _claude_dir_from(opts: Dict) -> Path:
    return Path(opts["claude_dir"]).expanduser() if opts.get("claude_dir") else find_claude_dir()


def _short(path) -> str:
    from claudepath.migrate import shorten

    return shorten(str(path))


def _run_sync(claude_dir: Path, dry_run: bool):
    from claudepath import registry as reg_mod

    reg = reg_mod.load_registry(claude_dir)
    paths = reg_mod.known_project_paths(claude_dir, find_claude_json(claude_dir))
    result = reg_mod.sync_registry(reg, paths, with_git=True)
    if not dry_run:
        reg_mod.save_registry(claude_dir, reg)
    return reg, result


def cmd_sync(args: list) -> None:
    from claudepath import registry as reg_mod

    opts, _ = _parse_opts(args, "sync", ("--dry-run", "--verbose"), ("--claude-dir",))
    claude_dir = _claude_dir_from(opts)
    dry_run = bool(opts.get("dry_run"))
    if dry_run:
        print(_c("DRY RUN — no files will be modified", YELLOW, BOLD))
        print()
    if not claude_dir.is_dir():
        print_error(f"Claude data directory not found: {claude_dir}")
        sys.exit(1)
    if dry_run:
        reg, result = _run_sync(claude_dir, dry_run=True)
    else:
        with reg_mod.registry_lock(claude_dir, timeout=10.0):
            reg, result = _run_sync(claude_dir, dry_run=False)
    _print_sync_result(claude_dir, reg, result, dry_run, bool(opts.get("verbose")))


def _print_sync_result(claude_dir: Path, reg: Dict, result, dry_run: bool, verbose: bool) -> None:
    from claudepath.registry import registry_path

    verb = "Would record" if dry_run else "Recorded"
    print(f"  {_c('Registry:', BOLD)} {_short(registry_path(claude_dir))}")
    print(
        f"  {verb} {len(result.added)} new project(s), refreshed {len(result.refreshed)}; "
        f"{len(reg['projects'])} tracked in total"
    )
    if verbose:
        for p in result.added:
            print(f"    {_c('+', GREEN)} {_short(p)}")
        for p in result.refreshed:
            print(f"    {_c('✓', GREEN)} {_short(p)}")
    for p in result.changed:
        print(f"    {_c('!', YELLOW)} {_short(p)} {_c('— a different folder is there now; left for auto', DIM)}")
    if result.missing:
        print(
            f"  {len(result.missing)} known path(s) no longer exist "
            f"{_c('(see claudepath status; rescue with claudepath scan <root>)', DIM)}"
        )
        if verbose:
            for p in result.missing:
                print(f"    {_c('✗', RED)} {_short(p)}")


def cmd_install(args: list) -> None:
    import shutil

    from claudepath import shellrc
    from claudepath.backup import Journal
    from claudepath.fsutil import atomic_write

    opts, _ = _parse_opts(
        args, "install", ("--dry-run", "--yes"), ("--shell", "--rc", "--claude-dir")
    )
    try:
        shell = shellrc.detect_shell(opts.get("shell"))
    except ValueError as e:
        print_error(str(e))
        sys.exit(1)
    rc = Path(opts["rc"]).expanduser() if opts.get("rc") else shellrc.default_rc(shell)
    claude_dir = _claude_dir_from(opts)
    dry_run = bool(opts.get("dry_run"))
    text = rc.read_text(encoding="utf-8") if rc.exists() else ""
    try:
        new_text = shellrc.with_block(text, shell)
    except ValueError as e:
        print_error(f"{rc}: {e}")
        sys.exit(1)

    if dry_run:
        print(_c("DRY RUN — no files will be modified", YELLOW, BOLD))
        print()
    print(f"  {_c('Shell:', BOLD)} {shell}")
    print(f"  {_c('File: ', BOLD)} {rc}")
    print()
    if new_text == text:
        print("  The claudepath block is already installed and up to date.")
        return
    action = "update the block in" if shellrc.find_block(text) else "append this block to"
    print(f"  {_c(f'Will {action} {rc.name}:', DIM)}")
    for line in shellrc.render_block(shell).splitlines():
        print(f"    {line}")
    print()
    before, after = shellrc.wrappers_outside_block(new_text)
    if before:
        print(f"  {_c('Note:', CYAN)} your existing claude function/alias stays in charge of "
              "launching claude; claudepath auto just runs first.")
    if after:
        print(f"  {_c('Warning:', YELLOW)} {rc.name} defines claude again after the block, "
              "which would replace the wrapper. Move that definition above the block.")
    if shutil.which("claudepath") is None:
        print(f"  {_c('Warning:', YELLOW)} 'claudepath' is not on PATH here; the wrapper "
              "silently skips auto until it is.")
    if shell == "bash":
        warning = shellrc.bash_profile_warning()
        if warning:
            print(f"  {_c('Warning:', YELLOW)} {warning}")
    if dry_run:
        print("  Would also record existing projects ('claudepath sync').")
        return
    print()
    if not opts.get("yes") and not confirm(f"Update {rc}?"):
        print("Aborted.")
        return

    journal = Journal(get_journal_base(claude_dir), "install", {"rc": str(rc)})
    existed = rc.exists()
    journal.save(rc)
    atomic_write(rc, new_text.encode("utf-8"))
    if not existed:
        os.chmod(rc, 0o644)
    journal.finish()
    print(_c("Installed.", GREEN, BOLD), _c(f"(backup of {rc.name}: {journal.dir})", DIM))

    if claude_dir.is_dir():
        from claudepath import registry as reg_mod

        print()
        print("  Recording existing projects (claudepath sync)...")
        with reg_mod.registry_lock(claude_dir, timeout=10.0):
            reg, result = _run_sync(claude_dir, dry_run=False)
        _print_sync_result(claude_dir, reg, result, False, False)
    print()
    print(f"  Open a new terminal, or run:  source {rc}")


def cmd_uninstall(args: list) -> None:
    from claudepath import shellrc
    from claudepath.backup import Journal
    from claudepath.fsutil import atomic_write

    opts, _ = _parse_opts(
        args, "uninstall", ("--dry-run", "--yes"), ("--shell", "--rc", "--claude-dir")
    )
    if opts.get("rc"):
        rc = Path(opts["rc"]).expanduser()
    else:
        try:
            rc = shellrc.default_rc(shellrc.detect_shell(opts.get("shell")))
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
    text = rc.read_text(encoding="utf-8") if rc.exists() else ""
    try:
        new_text = shellrc.without_block(text)
    except ValueError as e:
        print_error(f"{rc}: {e}")
        sys.exit(1)
    if new_text is None:
        print(f"  No claudepath block in {rc}; nothing to do.")
        return
    dry_run = bool(opts.get("dry_run"))
    if dry_run:
        print(_c("DRY RUN — no files will be modified", YELLOW, BOLD))
        print()
    start, end = shellrc.find_block(text)
    print(f"  {_c('Will remove from', DIM)} {rc}:")
    for line in text[start:end].splitlines():
        print(f"    {line}")
    print()
    if dry_run:
        return
    if not opts.get("yes") and not confirm(f"Remove the block from {rc}?"):
        print("Aborted.")
        return
    journal = Journal(get_journal_base(_claude_dir_from(opts)), "uninstall", {"rc": str(rc)})
    journal.save(rc)
    atomic_write(rc, new_text.encode("utf-8"))
    journal.finish()
    print(_c("Removed.", GREEN, BOLD), _c(f"(backup of {rc.name}: {journal.dir})", DIM))
    print("  Open a new terminal (or run 'exec $SHELL') for it to take effect.")
    print("  The registry and backups in ~/.claude/claudepath/ were kept.")


def cmd_scan(args: list) -> None:
    from claudepath import registry as reg_mod
    from claudepath.auto import _claude_processes_in
    from claudepath.migrate import MigrationError, migrate, recover_interrupted
    from claudepath.orphans import find_orphans, index_tree, match_orphans

    opts, positional = _parse_opts(
        args, "scan", ("--dry-run", "--yes", "--verbose"), ("--claude-dir",), max_positional=1
    )
    if not positional:
        print_error("scan needs a <root> directory to search, e.g. 'claudepath scan ~'")
        sys.exit(1)
    root = os.path.realpath(os.path.expanduser(positional[0]))
    if not os.path.isdir(root):
        print_error(f"Not a directory: {root}")
        sys.exit(1)
    claude_dir = _claude_dir_from(opts)
    claude_json = find_claude_json(claude_dir)
    dry_run = bool(opts.get("dry_run"))
    if dry_run:
        print(_c("DRY RUN — no files will be modified", YELLOW, BOLD))
        print()

    reg = reg_mod.load_registry(claude_dir)
    orphans, _unknown = find_orphans(claude_dir, claude_json, reg)
    if not orphans:
        print("  No orphaned projects — every session directory's folder exists.")
        return
    print(f"  Looking under {_short(root)} for {len(orphans)} orphaned project(s)...")
    by_inode, by_name = index_tree(root, claude_dir)
    matches, unmatched = match_orphans(orphans, by_inode, by_name)
    for m in matches:
        if m.kind == "inode" and reg_mod.is_declined(reg, m.orphan.path, m.target):
            m.kind, m.note = "name", "same folder, but you undid this move before"
    print()
    for m in matches:
        mark = _c("✓", GREEN) if m.kind == "inode" else _c("?", YELLOW)
        target_sessions = sum(1 for _ in reg_mod.project_dir_for(claude_dir, m.target).glob("*.jsonl"))
        merge = f", merges with its {target_sessions} session(s)" if target_sessions else ""
        print(f"  {mark} {_short(m.orphan.path)}  →  {_c(_short(m.target), BOLD)}")
        print(f"      {_c(f'{m.note}; {m.orphan.sessions} session(s){merge}', DIM)}")
    for o, why in unmatched:
        print(f"  {_c('✗', RED)} {_short(o.path)}  {_c(f'({o.sessions} session(s)) — {why}', DIM)}")
    print()
    if not matches:
        print("  Nothing to rescue under this root.")
        return
    if dry_run:
        print(f"  {_c('✓', GREEN)} = moved without asking, {_c('?', YELLOW)} = asks first (or --yes).")
        return

    moved = 0
    with reg_mod.registry_lock(claude_dir, timeout=10.0):
        for journal_dir in recover_interrupted(claude_dir):
            print(f"  Rolled back an interrupted migration (backup {journal_dir.name}).")
        for m in matches:
            if not m.orphan.project_dir.exists():
                continue  # carried along with a parent project
            running = _claude_processes_in(m.target, m.orphan.path)
            if running:
                print(f"  Skipped {_short(m.orphan.path)}: a Claude session (pid {running[0]}) "
                      "is running in it; try again after it exits.")
                continue
            if m.kind == "name" and not opts.get("yes"):
                if not sys.stdin.isatty():
                    print(f"  Skipped {_short(m.orphan.path)} (same-name match: confirm in a "
                          "terminal or pass --yes)")
                    continue
                if not confirm(f"Move sessions of {_short(m.orphan.path)} to {_short(m.target)}?"):
                    continue
            try:
                result = migrate(
                    m.orphan.path, m.target, claude_dir, claude_json,
                    reason=f"scan ({m.kind} match)", verbose=bool(opts.get("verbose")),
                )
            except MigrationError as e:
                print_error(str(e))
                continue
            moved += 1
            print(_c(f"Moved {_short(m.orphan.path)} → {_short(m.target)}", GREEN, BOLD))
            print(result.summary())
    print()
    print(f"  {moved} project(s) rescued.")


def _claude_code_version() -> Optional[str]:
    import shutil

    exe = shutil.which("claude")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{out.splitlines()[0] if out else '?'}  ({_short(exe)})"


def encoding_check(claude_dir: Path) -> Tuple[int, List[Tuple[str, str]], int]:
    """Compare every project directory name with encode_path() of the cwd its
    sessions recorded. Returns (matching dirs, [(dir, path) mismatches],
    dirs without any recorded cwd)."""
    from claudepath.encoder import encode_path
    from claudepath.scanner import project_paths_in_dir

    ok, mismatches, unverifiable = 0, [], 0
    projects = claude_dir / "projects"
    if not projects.is_dir():
        return 0, [], 0
    for d in sorted(projects.iterdir()):
        if not d.is_dir():
            continue
        paths = project_paths_in_dir(d)
        if not paths:
            unverifiable += 1
            continue
        bad = [p for p in paths if encode_path(p) != d.name]
        if bad:
            mismatches.extend((d.name, p) for p in bad)
        else:
            ok += 1
    return ok, mismatches, unverifiable


def cmd_status(args: list) -> None:
    from claudepath import registry as reg_mod
    from claudepath import shellrc
    from claudepath.encoder import encode_path
    from claudepath.orphans import find_orphans, locate_inode

    opts, _ = _parse_opts(args, "status", ("--dry-run", "--verbose"), ("--claude-dir", "--rc"))
    claude_dir = _claude_dir_from(opts)
    claude_json = find_claude_json(claude_dir)
    projects_dir = claude_dir / "projects"
    n_dirs = sum(1 for d in projects_dir.iterdir() if d.is_dir()) if projects_dir.is_dir() else 0

    print(_c("claudepath status\n", BOLD))
    print(f"  {_c('Claude Code', BOLD)}  {_claude_code_version() or 'not found on PATH'}")
    print(f"  {_c('Data dir', BOLD)}     {_short(claude_dir)}  ({n_dirs} project dirs)")
    print(f"  {_c('Config', BOLD)}       {_short(claude_json)}")

    # Shell hook
    try:
        rc = Path(opts["rc"]).expanduser() if opts.get("rc") else shellrc.default_rc(shellrc.detect_shell())
    except ValueError:
        rc = None
    if rc is None:
        hook = _c("unknown shell — pass --rc", YELLOW)
    else:
        text = rc.read_text(encoding="utf-8") if rc.exists() else ""
        try:
            block = shellrc.find_block(text)
        except ValueError as e:
            hook = _c(f"damaged block in {_short(rc)}: {e}", RED)
        else:
            if block is None:
                hook = _c(f"not installed in {_short(rc)} (claudepath install)", YELLOW)
            else:
                before, after = shellrc.wrappers_outside_block(text)
                hook = _c(f"installed in {_short(rc)}", GREEN)
                if before:
                    hook += _c(" — chains your existing claude wrapper", DIM)
                if after:
                    hook += _c(" — but a later 'claude' definition overrides it!", RED)
    print(f"  {_c('Shell hook', BOLD)}   {hook}")
    print()

    # Slug encoding
    ok, mismatches, unverifiable = encoding_check(claude_dir)
    print(_c("  Slug encoding (~/.claude/projects names vs. session cwd)", BOLD))
    if mismatches:
        print(f"    {_c('✗', RED)} {len(mismatches)} mismatch(es) — claudepath may not find these sessions:")
        for name, path in mismatches:
            print(f"      {name} holds sessions for {path} (expected {encode_path(path)})")
    if ok:
        print(f"    {_c('✓', GREEN)} {ok} project dir(s) match exactly")
    if unverifiable:
        print(f"    {_c('·', DIM)} {unverifiable} dir(s) have no session cwd to check against")
    print()

    # Tracked projects
    reg = reg_mod.load_registry(claude_dir)
    entries = reg["projects"]
    print(_c(f"  Tracked projects ({len(entries)}) — {_short(reg_mod.registry_path(claude_dir))}", BOLD))
    if not entries:
        print(f"    none yet — run {_c('claudepath sync', BOLD)}")
    for path, entry in sorted(entries.items()):
        sessions = sum(1 for _ in reg_mod.project_dir_for(claude_dir, path).glob("*.jsonl"))
        count = _c(f"{sessions} session(s)", DIM) if sessions else ""
        try:
            st = os.stat(path)
        except OSError:
            st = None
        if st is not None and reg_mod.same_identity(entry, st):
            print(f"    {_c('✓', GREEN)} {_short(path)}  {count}")
            continue
        now = locate_inode(entry.get("dev", -1), entry.get("ino", -1)) if "ino" in entry else None
        if now and now != path:
            print(f"    {_c('→', CYAN)} {_short(path)}  {count}")
            print(f"        {_c('moved to', DIM)} {_short(now)} "
                  f"{_c('— sessions follow on the next claude launch there', DIM)}")
        elif st is not None:
            print(f"    {_c('!', YELLOW)} {_short(path)}  {count} "
                  f"{_c('— a different folder is there now', DIM)}")
        else:
            print(f"    {_c('✗', RED)} {_short(path)}  {count} {_c('— gone', DIM)}")
    print()

    # Orphans
    orphans, unknown = find_orphans(claude_dir, claude_json, reg)
    print(_c(f"  Orphaned session dirs ({len(orphans)}) — folder no longer at the recorded path", BOLD))
    for o in orphans:
        print(f"    {_c('✗', RED)} {_short(o.path)}  {_c(f'{o.sessions} session(s)', DIM)}")
    if orphans:
        print(f"    {_c('Rescue with: claudepath scan <folder that contains them> --dry-run', DIM)}")
    if unknown:
        print(f"    {_c(f'{len(unknown)} dir(s) without any recorded project path: ' + ', '.join(d.name for d in unknown), DIM)}")
    print()

    # Backups
    journals = list_journals(get_journal_base(claude_dir))
    print(f"  {_c('Backups', BOLD)}      {len(journals)} in {_short(get_journal_base(claude_dir))}"
          + (_c("  (hard-linked session files share space with live ones)", DIM) if journals else ""))


# ─── Update ────────────────────────────────────────────────────────────────

def detect_install_method() -> str:
    """Detect how claudepath was installed. Returns 'brew', 'pipx', or 'pip'."""
    pkg_path = os.path.realpath(__file__).lower()
    if "/homebrew/" in pkg_path or "/cellar/" in pkg_path:
        return "brew"
    if "/pipx/" in pkg_path:
        return "pipx"
    return "pip"


def cmd_update(args: list) -> None:
    if "--help" in args or "-h" in args:
        _print_help_update()
        return

    # Parse flags
    method_override = None
    for arg in args:
        if arg == "--brew":
            method_override = "brew"
        elif arg == "--pipx":
            method_override = "pipx"
        elif arg == "--pip":
            method_override = "pip"
        elif arg.startswith("--"):
            print_error(f"Unknown option: {arg}")
            sys.exit(1)

    print(f"  {_c('Current version:', BOLD)} {__version__}")
    print(f"  Checking for updates...")

    latest = check_latest_version()
    if latest is None:
        print_error("Could not check for updates. Check your internet connection.")
        sys.exit(1)

    if parse_version(latest) <= parse_version(__version__):
        print(f"\n  {_c('Already up to date!', GREEN, BOLD)}")
        return

    method = method_override or detect_install_method()

    print(f"  {_c('New version:', BOLD)} {latest}")
    print(f"  {_c('Install method:', BOLD)} {method}")
    print()

    if method == "brew":
        cmd = ["brew", "upgrade", "claudepath"]
    elif method == "pipx":
        cmd = ["pipx", "upgrade", "claudepath"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "claudepath"]

    print(f"  {_c('Running:', DIM)} {' '.join(cmd)}\n")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"\n  {_c(f'Updated to v{latest}!', GREEN, BOLD)}")
    else:
        print_error(f"Update failed (exit code {result.returncode})")
        sys.exit(1)


# ─── Version Check ─────────────────────────────────────────────────────────


def parse_version(version: str) -> tuple:
    """Parse a version string like '1.2.3' into a tuple of ints for comparison."""
    return tuple(int(x) for x in version.split("."))


def check_latest_version() -> Optional[str]:
    """Fetch the latest version from PyPI. Returns version string or None on failure."""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/claudepath/json",
            headers={"User-Agent": f"claudepath/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = _json.loads(resp.read())
            return data["info"]["version"]
    except (OSError, urllib.error.URLError, ValueError, KeyError):
        return None


def _print_update_notice(latest: str) -> None:
    print(
        f"\n{_c('⚠', YELLOW, BOLD)}  {_c(f'New version available: {latest}', YELLOW)} "
        f"{_c(f'(you have {__version__})', DIM)}"
    )
    print(f"   {_c('claudepath update', BOLD)}  {_c('# update automatically', DIM)}")


# ─── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("help", "--help", "-h"):
        print_help()
        return

    if args[0] == "--version":
        print(f"claudepath {__version__}")
        return

    command = args[0]
    rest = args[1:]

    # auto runs before every claude launch: no update check, minimal imports
    if command == "auto":
        from claudepath.auto import main as auto_main

        sys.exit(auto_main(rest))

    # update and restore handle their own flow — skip background check
    if command == "update":
        cmd_update(rest)
        return
    if command == "restore":
        cmd_restore(rest)
        return

    # Check for updates in background — does not block command execution
    latest_version: list = []
    checker = threading.Thread(
        target=lambda: latest_version.append(check_latest_version()),
        daemon=True,
    )
    checker.start()

    if command == "mv":
        cmd_mv(rest)
    elif command == "remap":
        cmd_remap(rest)
    elif command == "list":
        cmd_list(rest)
    elif command == "sync":
        cmd_sync(rest)
    elif command == "install":
        cmd_install(rest)
    elif command == "uninstall":
        cmd_uninstall(rest)
    elif command == "scan":
        cmd_scan(rest)
    elif command == "status":
        cmd_status(rest)
    else:
        import difflib

        matches = difflib.get_close_matches(command, ALL_COMMANDS, n=1, cutoff=0.6)
        if matches:
            print_error(f"Unknown command: '{command}'. Did you mean '{matches[0]}'?")
        else:
            print_error(f"Unknown command: '{command}'")
        print("Run 'claudepath help' for usage.", file=sys.stderr)
        sys.exit(1)

    # Wait up to 2s for the version check, then print notice if outdated
    checker.join(timeout=2)
    if (latest_version and latest_version[0]
            and parse_version(latest_version[0]) > parse_version(__version__)):
        _print_update_notice(latest_version[0])


if __name__ == "__main__":
    main()
