"""Validate and apply the unified diff returned by the coder model."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
_META_PREFIXES = ("diff --git", "index ", "new file mode", "deleted file mode", "similarity", "rename ")
_FENCE_RE = re.compile(r"^```\w*\s*$")


class PatchError(ValueError):
    """A hunk does not apply to the file it targets."""


class DiffQualityError(RuntimeError):
    """The model's diff failed the quality gate."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = issues


@dataclass
class _Hunk:
    old_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _FilePatch:
    path: str
    is_new: bool
    hunks: list[_Hunk] = field(default_factory=list)


@dataclass
class DiffReport:
    ok: bool
    issues: list[str]
    diff: str = ""
    files: dict[str, str] = field(default_factory=dict)
    files_changed: list[str] = field(default_factory=list)


def _strip_fences(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if not _FENCE_RE.match(ln)]
    return "\n".join(lines)


def _clean_path(raw: str) -> str:
    raw = raw.split("\t")[0].strip()
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return raw


def _trim_blank_context(hunk: _Hunk | None) -> None:
    # Blank context at the end of a hunk is usually a separator between file diffs.
    if hunk is not None:
        while hunk.lines and hunk.lines[-1] == (" ", ""):
            hunk.lines.pop()


def _parse(diff: str) -> tuple[list[_FilePatch], list[str]]:
    patches: list[_FilePatch] = []
    issues: list[str] = []
    lines = diff.splitlines()
    current: _FilePatch | None = None
    hunk: _Hunk | None = None

    i = 0
    while i < len(lines):
        line = lines[i]
        # A "--- " line only starts a file header when "+++ " follows; a removed
        # line whose content starts with "-- " looks the same otherwise.
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            _trim_blank_context(hunk)
            hunk = None
            old = line[4:].split("\t")[0].strip()
            new = lines[i + 1][4:].split("\t")[0].strip()
            if new == "/dev/null":
                issues.append(f"file deletion is not supported: {old}")
                current = None
            else:
                current = _FilePatch(path=_clean_path(new), is_new=old == "/dev/null")
                patches.append(current)
            i += 2
            continue

        match = _HUNK_RE.match(line)
        if match and current is not None:
            _trim_blank_context(hunk)
            hunk = _Hunk(old_start=int(match.group(1)))
            current.hunks.append(hunk)
        elif line.startswith(_META_PREFIXES) or line.startswith("\\"):
            pass
        elif hunk is not None and line[:1] in (" ", "+", "-"):
            hunk.lines.append((line[0], line[1:]))
        elif hunk is not None and line == "":
            hunk.lines.append((" ", ""))
        elif line.strip():
            issues.append(f"unexpected text outside the diff: {line[:80]!r}")
        i += 1

    _trim_blank_context(hunk)
    return patches, issues


def _find(lines: list[str], old: list[str], start: int, hint: int) -> int | None:
    if not old:
        return min(max(hint, start), len(lines))

    def matches(at: int) -> bool:
        if at < start or at + len(old) > len(lines):
            return False
        return all(lines[at + n].rstrip() == old[n].rstrip() for n in range(len(old)))

    # Models get hunk line numbers wrong often; trust them only when the content matches.
    if matches(hint):
        return hint
    for at in range(start, len(lines) - len(old) + 1):
        if matches(at):
            return at
    return None


def _apply(original: str | None, patch: _FilePatch) -> str:
    lines = original.splitlines() if original else []
    ends_with_newline = original.endswith("\n") if original else True
    out: list[str] = []
    pos = 0

    for hunk in patch.hunks:
        old = [text for op, text in hunk.lines if op != "+"]
        new = [text for op, text in hunk.lines if op != "-"]
        at = _find(lines, old, pos, hunk.old_start - 1)
        if at is None:
            raise PatchError(f"hunk @@ -{hunk.old_start} does not apply to {patch.path}")
        out.extend(lines[pos:at])
        out.extend(new)
        pos = at + len(old)

    out.extend(lines[pos:])
    text = "\n".join(out)
    return text + "\n" if out and ends_with_newline else text


class DiffQualityChecker:
    """Checks that a diff is well-formed, in scope, and applies cleanly."""

    def __init__(self, allowed_paths: set[str] | None = None) -> None:
        self._allowed = allowed_paths

    def check(self, diff: str, read_original: Callable[[str], str | None]) -> DiffReport:
        diff = _strip_fences(diff)
        if not diff.strip():
            return DiffReport(ok=False, issues=["empty diff"])

        patches, issues = _parse(diff)
        if not patches:
            issues.append("no file headers (--- / +++) found")
        files: dict[str, str] = {}

        for patch in patches:
            if patch.path.startswith("/") or ".." in patch.path.split("/") or patch.path.startswith(".git/"):
                issues.append(f"unsafe path: {patch.path}")
                continue
            if self._allowed is not None and patch.path not in self._allowed:
                issues.append(f"file not in approved plan: {patch.path}")
                continue
            if not patch.hunks:
                issues.append(f"no hunks for {patch.path}")
                continue
            original = None if patch.is_new else read_original(patch.path)
            if not patch.is_new and original is None:
                issues.append(f"file to modify does not exist: {patch.path}")
                continue
            try:
                files[patch.path] = _apply(original, patch)
            except PatchError as exc:
                issues.append(str(exc))

        return DiffReport(
            ok=not issues,
            issues=issues,
            diff=diff,
            files=files,
            files_changed=[p.path for p in patches if p.path in files],
        )
