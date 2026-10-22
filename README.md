# claudepath

[![CI](https://github.com/Mahiler1909/claudepath/actions/workflows/ci.yml/badge.svg)](https://github.com/Mahiler1909/claudepath/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/Mahiler1909/claudepath/branch/main/graph/badge.svg)](https://codecov.io/gh/Mahiler1909/claudepath)
[![PyPI version](https://img.shields.io/pypi/v/claudepath)](https://pypi.org/project/claudepath/)
[![Downloads](https://img.shields.io/pypi/dm/claudepath)](https://pypi.org/project/claudepath/)
[![Python versions](https://img.shields.io/pypi/pyversions/claudepath)](https://pypi.org/project/claudepath/)
[![License](https://img.shields.io/github/license/Mahiler1909/claudepath)](LICENSE)

Move or rename [Claude Code](https://claude.ai/claude-code) projects without losing session history, memory, and context.

## The Problem

When you move or rename a project directory, Claude Code loses all your session history because sessions are keyed to the absolute path of your project. You end up with orphaned data in `~/.claude/projects/` and a fresh start.

**claudepath** fixes this by updating all path references in Claude Code's data files after a move — either when you ask (`claudepath mv` / `remap`), or automatically: with the [automatic mode](#automatic-mode-sessions-follow-your-folders) installed, you just move or rename folders in Finder or the terminal and your sessions follow.

## Installation

```bash
# With pipx (recommended — isolated install)
pipx install claudepath

# With pip
pip install claudepath
```

### Homebrew (macOS/Linux)

```bash
brew tap Mahiler1909/tools
brew install claudepath
```

## Automatic mode: sessions follow your folders

```bash
claudepath install     # once: adds a small block to ~/.zshrc (asks first), records your projects
```

From then on, every `claude` launch first runs `claudepath auto`, which notices when the current folder (or one of its parents) is a project that was moved or renamed, and moves its Claude Code data along before Claude Code looks for it. It prints nothing unless it moves something, takes ~50 ms when nothing moved, and never stops `claude` from starting — on any error it prints one line and gets out of the way.

```
$ mv ~/code/shop ~/clients/acme/shop-v2
$ cd ~/clients/acme/shop-v2 && claude
claudepath: sessions followed your folder: ~/code/shop → ~/clients/acme/shop-v2 (14 sessions; backup 20261022_104200)
```

### How a moved folder is recognised

`claudepath sync` (run by `install`) records an identity for every project folder in `~/.claude/claudepath/registry.json`: its device and inode, its creation time (macOS), the repository's root commit, its path inside the repository, and the `origin` remote. `claudepath auto` refreshes the entry for the folder you launch in.

| Match | When | What happens |
|---|---|---|
| **Strong** | A registry entry's device+inode now belongs to the current folder (or a parent), the entry's path no longer refers to that folder, and the folder's creation time (macOS) or git root commit confirms it is the same one. This is what a rename or a move within the same volume looks like — including a case-only rename. | Migrated without asking. An inode with nothing to confirm it — possible on Linux, which reuses the inode numbers of deleted folders — is treated like a weak match. |
| **Weak** | A move to another volume gives the folder a new inode. If the current folder has no sessions yet and exactly **one** registry entry whose path is gone has the same git root commit (and path inside the repo), it is probably the same project. | You're asked `[y/N]` — only in a terminal, never in scripts. A "no" is remembered. |
| None | Anything else. | Nothing is moved. claudepath never moves data on a guess. |

### Commands

```bash
claudepath install [--shell zsh|bash] [--rc FILE] [--dry-run] [--yes]
claudepath uninstall [--dry-run] [--yes]
claudepath sync [--dry-run] [--verbose]       # record/refresh project identities
claudepath auto [--dry-run] [--verbose]       # what the shell hook runs
claudepath status                             # tracked projects, orphans, hook, encoding check
claudepath scan <root> [--dry-run] [--yes]    # rescue sessions of folders moved before install
```

**`install`** appends this (marked) block to `~/.zshrc` — or `~/.bashrc` — after showing it to you. It is in effect `claude() { claudepath auto || true; command claude "$@"; }`, except that an existing `claude` function or alias (say `claude() { caffeinate -d claude "$@"; }`) keeps working and is called after `auto`, a self-referencing alias can't recurse, re-sourcing the file is harmless, and nothing breaks if claudepath is uninstalled. The old file is backed up first. `uninstall` removes the block.

**`status`** shows the tracked projects (and, on macOS, where a moved one went), orphaned session directories, whether the shell hook is installed, and checks that your Claude Code version names `~/.claude/projects` directories the way claudepath computes them.

**`scan <root>`** rescues sessions whose folder was already moved before claudepath knew about it. For each orphaned project it looks under `<root>` for the folder with the inode the registry recorded and confirmed by its creation time or git history (moved without asking), else for the **only** folder with the same name (asks first; `--yes` accepts; a different git history rules it out). Start with `--dry-run`:

```bash
claudepath scan ~ --dry-run
```

### What the automatic mode updates

| File / Directory | What changes |
|---|---|
| `~/.claude/projects/{encoded-dir}/` | Renamed — for the project **and every project started in a subfolder of it**. If Claude Code already created a directory at the new location, the two are merged without overwriting anything: identical files are deduplicated, a differing file with the same name (e.g. `memory/MEMORY.md`) is kept alongside as `MEMORY.claudepath-conflict-<timestamp>.md` |
| `{session}.jsonl` (incl. subagents) | Only the top-level `cwd` field. Every other byte of every line is left exactly as it was (the value is spliced in place, lines are never re-serialised), and file mtimes are preserved so `--resume` ordering doesn't change |
| `sessions-index.json` | Paths updated (older Claude Code versions) |
| `~/.claude/history.jsonl` | The `project` field of matching entries |
| `~/.claude.json` | Keys of the `projects` map — trust, allowed tools, MCP servers — moved; on a clash both entries are merged and the old values win. Local checkouts in `githubRepoPaths` too. Written atomically with the file's permissions kept |
| `usage-data/session-meta/*.json` | `project_path` (older Claude Code versions) |

`~/.claude.json` and `history.jsonl` are read and written under the same lock Claude Code uses for them, so a running session can't interleave a write. If a Claude session is still running inside the moved folder, the migration waits until the next launch after it exits, rather than splitting that session in two.

### Safety

- **Backups**: every file is saved to `~/.claude/claudepath/backups/{timestamp}/` right before it changes, and every rename is logged. Session files are hard-linked rather than copied — they are always replaced through a temp file, so the link keeps the original bytes — which makes backing up gigabytes of history instant.
- **Rollback**: if any step fails — or the terminal is closed, or the process is killed mid-way — everything done so far is undone (an interrupted migration is rolled back at the next launch, keeping anything written in between).
- **Undo**: `claudepath restore` puts a migration's data back under the old path *without* losing what was written since: sessions continued after the move keep their new lines, sessions started at the new path stay there, prompts typed since stay in history, and setting changes are carried back. `auto` remembers the undo and won't redo it. If the undo can't be done cleanly — say Claude Code has been used at the old path again, or a session is still running — it says why and changes nothing.
- **Concurrent writes**: lines Claude Code appends to a session while it is being rewritten are carried over.
- **`--dry-run`** on every command shows what would happen and writes nothing.

### Limitations

- The hook runs when you start `claude` from a shell. Launching Claude Code from an IDE or the desktop app skips it; run `claudepath auto` in the folder (or `claudepath scan`) to catch up.
- Weak matches (moves across volumes) need the folder to be a git repository with at least one commit.
- On Linux there is no folder creation time, so a moved folder is recognised silently only if it is a git repository (the root commit confirms the inode); otherwise you are asked first.
- zsh and bash are supported (bash 3.2+). On macOS, Terminal starts bash as a login shell: make sure `~/.bash_profile` sources `~/.bashrc` (`install` warns if it doesn't).

## Usage

### Move a project

Moves the directory on disk **and** updates all Claude Code references:

```bash
claudepath mv ~/old/location/my-project ~/new/location/my-project
```

### Remap after a manual move

If you already moved the directory yourself, just update Claude's references:

```bash
claudepath remap ~/old/location/my-project ~/new/location/my-project
```

### Preview changes (dry run)

See exactly what would change without modifying anything:

```bash
claudepath mv ~/old/path ~/new/path --dry-run
```

### List tracked projects

```bash
claudepath list
```

### Update claudepath

Update to the latest version — auto-detects whether you installed via Homebrew or pipx:

```bash
claudepath update
```

You can also force a specific method:

```bash
claudepath update --brew
claudepath update --pipx
claudepath update --pip
```

### Restore from backup

List and restore from automatic backups:

```bash
claudepath restore --list       # see available backups
claudepath restore              # restore the latest backup
claudepath restore 20260227_145300  # restore a specific backup
```

### Merge sessions when destination already has Claude data

If you opened Claude Code at the new location before running `claudepath remap`, Claude Code will have already created a new project directory there. Use `--merge` to combine the sessions from both directories:

```bash
claudepath remap ~/old/path ~/new/path --merge
```

Without `--merge`, claudepath will fail with a clear error suggesting you add the flag.

### Full help

```bash
claudepath help
claudepath mv --help
```

## What it updates

| File / Directory | What changes |
|---|---|
| `~/.claude/projects/{encoded-dir}/` | Renamed to match new path |
| `sessions-index.json` | `originalPath`, `projectPath`, `fullPath` updated (proper JSON parsing) |
| `{session}.jsonl` files | `cwd` and file path references updated (line-by-line, handles large files) |
| Subagent `.jsonl` files | Same as above, recursive |
| `~/.claude/history.jsonl` | `project` field updated for all matching entries |
| `usage-data/session-meta/*.json` | `project_path` updated (token usage & analytics) |

> **Note:** `file-history/`, `todos/`, `tasks/`, and `shell-snapshots/` are keyed by session UUID, not by project path — they don't need updating.
>
> `mv` and `remap` rewrite every path-valued string in the session files (not just `cwd`) and do not update `~/.claude.json`. The [automatic mode](#automatic-mode-sessions-follow-your-folders) also updates `~/.claude.json`, and fixes up whatever `mv`/`remap` left behind the next time you launch `claude` in the moved folder.

## Options

| Flag | Description |
|---|---|
| `--dry-run` | Preview all changes without writing anything |
| `--no-backup` | Skip creating a backup before modifying files |
| `--yes` / `-y` | Skip the confirmation prompt |
| `--merge` | Merge sessions when destination already has Claude data |
| `--verbose` / `-v` | Show detailed file-by-file processing output |
| `--claude-dir <path>` | Override the Claude data directory (default: `~/.claude`) |

## Environment Variables

| Variable | Description |
|---|---|
| `NO_COLOR` | Disable colored output (set to any value) |
| `FORCE_COLOR` | Force colored output even when not a TTY |
| `CLAUDE_CONFIG_DIR` | Claude Code's data directory, if you moved it (honoured like Claude Code does: `projects/`, `history.jsonl` and `.claude.json` all live there) |

## Backup & Rollback

By default, claudepath creates a backup before making any changes:

```
~/.claude/backups/claudepath/{timestamp}/
```

The backup includes:
- The full project data directory (`~/.claude/projects/{encoded}/`)
- `~/.claude/history.jsonl`
- Affected `usage-data/session-meta/*.json` files (token usage & analytics)
- When using `--merge`: both the source and destination project directories

If any step fails, claudepath automatically restores from the backup. Use `--no-backup` only if you already have your own backup or want to skip the extra time.

The automatic mode keeps its own, per-file backups in `~/.claude/claudepath/backups/{timestamp}/` (see [Safety](#safety)). `claudepath restore --list` shows both kinds; `claudepath restore [<timestamp>] [--dry-run]` restores either — for an automatic migration, as an undo that keeps everything written since.

## How Claude Code encodes paths

Claude Code stores project data in `~/.claude/projects/` using an encoded directory name: every character that isn't an ASCII letter or digit (`/`, `.`, `_`, `~`, spaces, non-ASCII, …) is replaced with `-`. Names longer than 200 characters are truncated and suffixed with a base-36 hash of the original path.

The path that gets encoded is the real path of the directory `claude` was started in, with symlinks resolved and Unicode NFC-normalised (Claude Code 2.1.x: `realpathSync(process.cwd()).normalize("NFC")`). Normalisation matters for non-ASCII names: a decomposed (NFD) Korean or accented name has more UTF-16 code units, hence more dashes. `claudepath status` checks this encoding against your real `~/.claude/projects` directories.

```
/Users/foo/my-project      →  -Users-foo-my-project
/Users/foo/local.tmp/proj  →  -Users-foo-local-tmp-proj
```

This means moving `/Users/foo/old-name` to `/Users/foo/new-name` requires:
1. Renaming `-Users-foo-old-name` to `-Users-foo-new-name`
2. Updating all path strings inside the data files

## License

MIT
