"""Tidy the model's changes before tests run: only the lines the diff touched, never whole files.

Two fixes, both small and safe:
- duplicate `from X import ...` lines for the same module are merged into the first one
  (e.g. an added `from decimal import ROUND_HALF_UP` next to an existing `from decimal import Decimal`);
- a top-level `def` / `class` gets exactly two blank lines above it (PEP 8).
A file that does not parse is left alone: the syntax check reports it.
"""
from __future__ import annotations

import ast
import difflib
import re

_FROM_IMPORT = re.compile(r"^from\s+([\w.]+)\s+import\s+([^()#\\]+?)\s*(#.*)?$")


def tidy_changed_files(files: dict[str, str], read_original) -> tuple[dict[str, str], list[str]]:
    """Returns (files, notes). Only .py files the change touched; each note says what was fixed."""
    result, notes = dict(files), []
    for path, text in files.items():
        if not path.endswith(".py"):
            continue
        original = read_original(path) or ""
        fixed, fixes = tidy_python(original, text)
        if fixes:
            result[path] = fixed
            notes.extend(f"{path}: {fix}" for fix in fixes)
    return result, notes


def tidy_python(original: str, new: str) -> tuple[str, list[str]]:
    fixes: list[str] = []
    try:
        ast.parse(new)
    except SyntaxError:
        return new, fixes
    lines = new.split("\n")
    lines = _merge_duplicate_imports(lines, _touched(original, "\n".join(lines)), fixes)
    lines = _blank_lines_before_defs(lines, _touched(original, "\n".join(lines)), fixes)
    result = "\n".join(lines)
    try:
        ast.parse(result)
    except SyntaxError:  # never hand back something worse than we got
        return new, []
    return result, fixes


def _touched(original: str, new: str) -> set[int]:
    """0-based line numbers in `new` that the change added or changed, plus the neighbours of deletions."""
    touched: set[int] = set()
    matcher = difflib.SequenceMatcher(None, original.split("\n"), new.split("\n"), autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            touched.update(range(j1, j2))
        elif tag == "delete":
            touched.update({j1 - 1, j1})
    return touched


def _merge_duplicate_imports(lines: list[str], touched: set[int], fixes: list[str]) -> list[str]:
    by_module: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        match = _FROM_IMPORT.match(line)
        if match and not match.group(3):  # leave commented import lines alone
            by_module.setdefault(match.group(1), []).append(i)
    drop: set[int] = set()
    for module, rows in by_module.items():
        new_rows = [i for i in rows if i in touched]
        if len(rows) < 2 or not new_rows:
            continue  # no duplicate, or all duplicates predate the change: not ours to reformat
        # Keep an existing (untouched) line if there is one, so old lines are only extended, never moved.
        keep = next((i for i in rows if i not in touched), rows[0])
        names = [n.strip() for n in _FROM_IMPORT.match(lines[keep]).group(2).split(",") if n.strip()]
        added: list[str] = []
        for i in rows:
            if i == keep:
                continue
            for name in (n.strip() for n in _FROM_IMPORT.match(lines[i]).group(2).split(",")):
                if name and name not in names + added:
                    added.append(name)
            drop.add(i)
        lines[keep] = f"from {module} import {', '.join(names + added)}"
        fixes.append(f"merged duplicate import from {module} ({', '.join(added) or 'identical'})")
    return [line for i, line in enumerate(lines) if i not in drop]


def _blank_lines_before_defs(lines: list[str], touched: set[int], fixes: list[str]) -> list[str]:
    tree = ast.parse("\n".join(lines))
    starts = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
            while first > 0 and lines[first - 1].lstrip().startswith("#"):  # comments belong to the def
                first -= 1
            starts.append((first, node.name))
    for first, name in sorted(starts, reverse=True):  # bottom-up: earlier indices stay valid
        gap_start = first
        while gap_start > 0 and not lines[gap_start - 1].strip():
            gap_start -= 1
        if gap_start == 0:
            continue  # nothing above the definition
        region = set(range(gap_start - 1, first + 1))
        if not region & touched or first - gap_start == 2:
            continue
        fixes.append(f"two blank lines before {name}() (was {first - gap_start})")
        lines[gap_start:first] = ["", ""]
    return lines


def unified_diff(files: dict[str, str], read_original) -> str:
    """A fresh a/ b/ unified diff for the (tidied) files, for the PR artifact and the reviewer."""
    parts = []
    for path in sorted(files):
        original = read_original(path)
        parts.extend(difflib.unified_diff(
            (original or "").splitlines(keepends=True), files[path].splitlines(keepends=True),
            fromfile="/dev/null" if original is None else f"a/{path}", tofile=f"b/{path}",
        ))
    return "".join(parts)
