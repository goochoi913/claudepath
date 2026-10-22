"""
Tests for the byte-preserving field rewriter used by the automatic mode.
"""

import json
import os
import threading

import pytest

from claudepath.updaters import (
    ConcurrentWriteError,
    find_top_level_string,
    remap_path,
    rewrite_jsonl_field,
    rewrite_line_field,
)
from tests.conftest import compact

OLD = "/Users/foo/old proj"
NEW = "/Volumes/Ext/new \"proj\" é"


def line(obj) -> bytes:
    return (compact(obj) + "\n").encode("utf-8")


# ─── remap_path ────────────────────────────────────────────────────────────

def test_remap_path_exact_and_descendant():
    assert remap_path(OLD, OLD, NEW) == NEW
    assert remap_path(OLD + "/src/a.py", OLD, NEW) == NEW + "/src/a.py"


def test_remap_path_ignores_siblings_sharing_a_prefix():
    assert remap_path(OLD + "-other", OLD, NEW) is None
    assert remap_path(OLD + "x/y", OLD, NEW) is None
    assert remap_path("/Users/foo", OLD, NEW) is None


def test_remap_path_matches_nfd_spelling():
    old = "/Users/foo/한글"
    nfd = "/Users/foo/한글"  # same name, decomposed
    assert remap_path(nfd, old, "/new") == "/new"


# ─── find_top_level_string ────────────────────────────────────────────────

def test_finds_top_level_cwd_after_large_nested_values():
    raw = line({"message": {"content": [{"input": {"cwd": "/nested"}}]},
                "attachment": {"cwd": "/also/nested"}, "cwd": OLD, "sessionId": "s"})
    start, end, value = find_top_level_string(raw, "cwd")
    assert value == OLD
    assert raw[start:end] == json.dumps(OLD, ensure_ascii=False).encode()


def test_ignores_nested_cwd_when_no_top_level_one():
    raw = line({"type": "x", "data": {"cwd": OLD}})
    assert find_top_level_string(raw, "cwd") is None


def test_ignores_escaped_json_inside_strings():
    raw = line({"content": f'"cwd":"{OLD}"', "stdout": compact({"cwd": OLD})})
    assert find_top_level_string(raw, "cwd") is None


def test_ignores_key_with_escaped_quote_before_cwd():
    raw = line({'x"cwd': OLD})
    assert find_top_level_string(raw, "cwd") is None


def test_handles_spaced_json_from_other_tools():
    raw = (json.dumps({"type": "user", "cwd": OLD}, ensure_ascii=False) + "\n").encode()
    assert find_top_level_string(raw, "cwd")[2] == OLD


def test_ignores_non_string_and_truncated_values():
    assert find_top_level_string(line({"cwd": None}), "cwd") is None
    truncated = line({"cwd": OLD, "message": "x"})[:-6]
    assert find_top_level_string(truncated, "cwd") is None


# ─── rewrite_line_field ────────────────────────────────────────────────────

def test_rewrite_changes_only_the_value_token():
    raw = line({"parentUuid": None, "message": {"input": {"cwd": OLD}}, "cwd": OLD + "/sub",
                "sessionId": "s", "version": "2.1.308"})
    out = rewrite_line_field(raw, "cwd", OLD, NEW)
    expected = json.loads(raw)
    expected["cwd"] = NEW + "/sub"
    assert json.loads(out) == expected
    assert json.loads(out)["message"]["input"]["cwd"] == OLD  # nested: untouched
    old_tok = json.dumps(OLD + "/sub", ensure_ascii=False).encode()
    new_tok = json.dumps(NEW + "/sub", ensure_ascii=False).encode()
    assert out.count(new_tok) == 1
    assert out.replace(new_tok, old_tok) == raw  # nothing but the token changed


def test_rewrite_leaves_unrelated_lines_alone():
    assert rewrite_line_field(line({"cwd": OLD + "-other"}), "cwd", OLD, NEW) is None
    assert rewrite_line_field(line({"cwd": "/elsewhere"}), "cwd", OLD, NEW) is None
    assert rewrite_line_field(line({"type": "last-prompt", "lastPrompt": OLD}), "cwd", OLD, NEW) is None


def test_rewrite_preserves_invalid_utf8_and_crlf_elsewhere():
    raw = b'{"blob":"\xff\xfe raw bytes","cwd":"' + OLD.encode() + b'","n":1}\r\n'
    out = rewrite_line_field(raw, "cwd", OLD, NEW)
    tok = json.dumps(NEW, ensure_ascii=False).encode()
    assert out == b'{"blob":"\xff\xfe raw bytes","cwd":' + tok + b',"n":1}\r\n'


# ─── rewrite_jsonl_field ───────────────────────────────────────────────────

def _write(path, lines, no_final_newline=False):
    data = b"".join(lines)
    if no_final_newline:
        data = data.rstrip(b"\n")
    path.write_bytes(data)
    os.chmod(path, 0o640)
    os.utime(path, ns=(1_600_000_000_123_456_789, 1_600_000_000_987_654_321))


def test_rewrite_file_keeps_other_lines_byte_identical_and_metadata(tmp_path):
    f = tmp_path / "s.jsonl"
    lines = [
        line({"type": "mode"}),
        line({"type": "user", "cwd": OLD}),
        line({"type": "user", "data": {"cwd": OLD}}),
        line({"type": "user", "cwd": OLD + "/a"}),
        line({"type": "user", "cwd": OLD}),
    ]
    _write(f, lines, no_final_newline=True)
    before = f.read_bytes().splitlines(keepends=True)

    assert rewrite_jsonl_field(f, "cwd", OLD, NEW) == 3

    after = f.read_bytes().splitlines(keepends=True)
    assert len(after) == len(before)
    assert after[0] == before[0] and after[2] == before[2]
    assert json.loads(after[1])["cwd"] == NEW
    assert json.loads(after[3])["cwd"] == NEW + "/a"
    assert not after[-1].endswith(b"\n")  # missing final newline stays missing
    st = f.stat()
    assert st.st_mtime_ns == 1_600_000_000_987_654_321
    assert oct(st.st_mode & 0o777) == oct(0o640)


def test_rewrite_file_without_matches_is_not_touched(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [line({"cwd": "/elsewhere", "content": OLD})])
    ino = f.stat().st_ino
    assert rewrite_jsonl_field(f, "cwd", OLD, NEW) == 0
    assert f.stat().st_ino == ino  # not even replaced


def test_rewrite_file_dry_run_counts_without_writing(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [line({"cwd": OLD}), line({"cwd": OLD})])
    before = f.read_bytes()
    assert rewrite_jsonl_field(f, "cwd", OLD, NEW, dry_run=True) == 2
    assert f.read_bytes() == before


def test_rewrite_file_calls_backup_hook_before_replacing(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [line({"cwd": OLD})])
    original = f.read_bytes()
    seen = []
    rewrite_jsonl_field(f, "cwd", OLD, NEW, before_write=lambda p: seen.append(p.read_bytes()))
    assert seen == [original]


def test_rewrite_file_carries_over_lines_appended_meanwhile(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [line({"cwd": OLD, "n": 1})])

    def append(text):
        with open(f, "ab") as fh:
            fh.write(line({"cwd": OLD, "n": text}))

    # One append lands in the critical window (as the backup is taken), the
    # other right after the final size check, just before the rename.
    import claudepath.updaters as updaters

    real_fstat = os.fstat
    calls = []

    def fstat_then_append(fd):
        st = real_fstat(fd)
        if not calls:
            calls.append(1)
            append("late")
        return st

    def before_write(path):
        append("window")
        updaters.os.fstat = fstat_then_append

    try:
        changed = rewrite_jsonl_field(f, "cwd", OLD, NEW, before_write=before_write)
    finally:
        updaters.os.fstat = real_fstat
    rows = [json.loads(l) for l in f.read_bytes().splitlines()]
    assert [r["n"] for r in rows] == [1, "window", "late"]
    assert all(r["cwd"] == NEW for r in rows) and changed == 3
    assert not list(tmp_path.glob(".*.claudepath-tmp"))


def test_rewrite_file_refuses_a_file_that_shrinks(tmp_path):
    f = tmp_path / "s.jsonl"
    _write(f, [line({"cwd": OLD}), line({"cwd": OLD})])

    def truncate(path):
        with open(path, "r+b") as fh:
            fh.truncate(5)

    with pytest.raises(ConcurrentWriteError):
        rewrite_jsonl_field(f, "cwd", OLD, NEW, before_write=truncate)
    assert len(f.read_bytes()) == 5  # left as the other writer made it
    assert not list(tmp_path.glob(".*.claudepath-tmp"))


def test_truncated_lines_are_linear_and_untouched():
    import time

    body = b'say \\"hi\\" ' * 20000  # 200 KB of escaped quotes, as in tool output
    complete = b'{"type":"user","cwd":"' + OLD.encode() + b'","message":{"content":"' + body + b'"}}\n'
    truncated = complete[: len(complete) * 3 // 4]  # a crash cut the line short
    start = time.perf_counter()
    assert rewrite_line_field(truncated, "cwd", OLD, NEW) is None
    assert rewrite_line_field(complete, "cwd", OLD, NEW) is not None
    assert time.perf_counter() - start < 0.5


def test_lone_surrogate_is_escaped_like_javascript():
    raw = b'{"cwd":"' + OLD.encode() + b'/\\ud83d","x":1}\n'
    out = rewrite_line_field(raw, "cwd", OLD, NEW)
    assert json.loads(out)["cwd"] == NEW + "/\ud83d"
    assert b"\\ud83d" in out


def test_rewrite_file_follows_symlink_and_keeps_it(tmp_path):
    real = tmp_path / "real.jsonl"
    _write(real, [line({"cwd": OLD})])
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert rewrite_jsonl_field(link, "cwd", OLD, NEW) == 1
    assert link.is_symlink()
    assert json.loads(real.read_bytes())["cwd"] == NEW


def test_partial_last_line_is_never_rewritten(tmp_path):
    # A concurrent append caught halfway: the fragment would parse as an object
    # whose nested "cwd" looks top-level.
    f = tmp_path / "s.jsonl"
    complete = line({"type": "user", "cwd": OLD})
    fragment = b'{"message":{"input":{"cwd":"' + OLD.encode() + b'"}'
    f.write_bytes(complete + fragment)
    assert rewrite_jsonl_field(f, "cwd", OLD, NEW) == 1
    data = f.read_bytes()
    assert data.endswith(fragment)  # untouched
    assert json.loads(data.splitlines()[0])["cwd"] == NEW
