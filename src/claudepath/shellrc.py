"""
Shell integration for the automatic mode: `claudepath install` / `uninstall`.

Adds a marked block to ~/.zshrc (or ~/.bashrc) that wraps `claude` so
`claudepath auto` runs before every launch -- in effect:

    claude() { claudepath auto || true; command claude "$@"; }

with three refinements:
  * an existing `claude` function or alias (say `claude() { caffeinate -d
    claude "$@"; }`) is kept and called after auto, instead of being silently
    replaced by the wrapper;
  * a guard variable sends any nested `claude` call straight to the binary, so
    a self-referencing alias (`alias claude='claude --flag'`) cannot recurse;
  * re-sourcing the rc file does not wrap the wrapper.
If claudepath is not on PATH the wrapper just runs claude.
"""

import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

BEGIN = "# >>> claudepath auto >>>"
END = "# <<< claudepath auto <<<"

_HEADER = """\
# Lets Claude Code sessions follow project folders you move or rename:
# runs `claudepath auto` before every `claude`. Managed by `claudepath install`;
# remove with `claudepath uninstall`. An existing claude alias/function is kept."""

_ZSH = r"""
if (( ${+aliases[claude]} )); then
  _claudepath_alias=${aliases[claude]}
  unalias claude  # first, so the alias isn't expanded inside the copy below
  eval "_claudepath_orig_claude() { ${_claudepath_alias} \"\$@\"; }"
  unset _claudepath_alias
elif (( ${+functions[claude]} )) && [[ ${functions[claude]} != *_claudepath_wrapped* ]]; then
  eval "_claudepath_orig_$(functions claude)"
fi
function claude {
  if [[ -n ${_claudepath_wrapped-} ]]; then command claude "$@"; return; fi
  local _claudepath_wrapped=1
  if command -v claudepath >/dev/null 2>&1; then claudepath auto || true; fi
  if (( ${+functions[_claudepath_orig_claude]} )); then
    _claudepath_orig_claude "$@"
  else
    command claude "$@"
  fi
}"""

_BASH = r"""
if alias claude >/dev/null 2>&1; then
  _claudepath_alias=$(alias claude)
  eval "_claudepath_alias=${_claudepath_alias#alias claude=}"
  unalias claude  # first, so the alias isn't expanded inside the copy below
  eval "_claudepath_orig_claude() { ${_claudepath_alias} \"\$@\"; }"
  unset _claudepath_alias
elif declare -F claude >/dev/null 2>&1 && [[ $(declare -f claude) != *_claudepath_wrapped* ]]; then
  eval "_claudepath_orig_$(declare -f claude)"
fi
function claude {
  if [[ -n ${_claudepath_wrapped-} ]]; then command claude "$@"; return; fi
  local _claudepath_wrapped=1
  if command -v claudepath >/dev/null 2>&1; then claudepath auto || true; fi
  if declare -F _claudepath_orig_claude >/dev/null 2>&1; then
    _claudepath_orig_claude "$@"
  else
    command claude "$@"
  fi
}"""

# A definition of `claude` (function or alias) in an rc file.
_WRAPPER_RE = re.compile(
    r"^[ \t]*(?:function[ \t]+claude\b|claude[ \t]*\([ \t]*\)|alias[ \t]+claude=)", re.M
)


def detect_shell(explicit: Optional[str] = None) -> str:
    """Return "zsh" or "bash" (from --shell, else $SHELL)."""
    name = explicit or os.path.basename(os.environ.get("SHELL", ""))
    if name not in ("zsh", "bash"):
        raise ValueError(
            f"unsupported shell {name or '(unknown)'!r}; use --shell zsh or --shell bash"
        )
    return name


def default_rc(shell: str) -> Path:
    if shell == "zsh":
        return Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc"
    return Path.home() / ".bashrc"


def render_block(shell: str) -> str:
    body = _ZSH if shell == "zsh" else _BASH
    return f"{BEGIN}\n{_HEADER}{body}\n{END}\n"


def find_block(text: str) -> Optional[Tuple[int, int]]:
    """Span of an installed block (markers included, plus its final newline).

    Raises ValueError if the markers were edited by hand (a start marker
    without an end marker, or two blocks): then neither installing nor
    removing can tell what belongs to the block.
    """
    start = text.find(BEGIN)
    if start < 0:
        if END in text:
            raise ValueError(f"found '{END}' without '{BEGIN}'; please fix the file by hand")
        return None
    end = text.find(END, start)
    if end < 0:
        raise ValueError(f"found '{BEGIN}' without '{END}'; please fix the file by hand")
    if text.find(BEGIN, start + 1) >= 0:
        raise ValueError("found more than one claudepath block; please remove the extras by hand")
    end += len(END)
    if text[end:end + 1] == "\n":
        end += 1
    return start, end


def with_block(text: str, shell: str) -> str:
    """`text` with the block replaced in place, or appended after a blank line."""
    block = render_block(shell)
    span = find_block(text)
    if span:
        return text[:span[0]] + block + text[span[1]:]
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    return text + block


def without_block(text: str) -> Optional[str]:
    """`text` with the block (and the blank line before it) removed; None if absent."""
    span = find_block(text)
    if not span:
        return None
    start, end = span
    if text[:start].endswith("\n\n"):
        start -= 1
    return text[:start] + text[end:]


def wrappers_outside_block(text: str) -> Tuple[bool, bool]:
    """(defined before the block, defined after it) for claude functions/aliases.

    A definition after the block replaces the wrapper when the rc file runs.
    """
    span = find_block(text)
    before_text = text[:span[0]] if span else text
    after_text = text[span[1]:] if span else ""
    return bool(_WRAPPER_RE.search(before_text)), bool(_WRAPPER_RE.search(after_text))


def bash_profile_warning() -> Optional[str]:
    """On macOS, Terminal starts bash as a login shell, which reads
    ~/.bash_profile and not ~/.bashrc unless the profile sources it."""
    if sys.platform != "darwin":
        return None
    profile = Path.home() / ".bash_profile"
    try:
        content = profile.read_text(encoding="utf-8")
    except OSError:
        return None
    if ".bashrc" in content:
        return None
    return (
        "~/.bash_profile does not source ~/.bashrc, so login shells (Terminal.app) "
        "won't see the wrapper. Add this line to ~/.bash_profile:\n"
        "    [ -f ~/.bashrc ] && . ~/.bashrc"
    )
