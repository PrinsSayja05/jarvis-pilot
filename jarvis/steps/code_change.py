"""Call jarvis-coder-large to produce a unified diff for the approved plan."""
from __future__ import annotations

import ast
import builtins
import logging
from pathlib import Path

from jarvis.clients.litellm_client import LiteLLMClient
from jarvis.config import JarvisConfig
from jarvis.models.code_change import CodeChange
from jarvis.models.plan import Plan
from jarvis.models.ticket import JiraTicket
from jarvis.steps.code_style import tidy_changed_files, unified_diff
from jarvis.steps.diff_quality import DiffQualityChecker, DiffQualityError
from jarvis.steps.read_repo import RepoMap

logger = logging.getLogger("jarvis.code_change")

_SYSTEM_PROMPT = "Du bist ein Coding-Agent. Antworte NUR mit einem unified diff. Kein Text davor oder danach."

# Initial attempt plus one retry with the quality-gate findings fed back to the model.
_MAX_DIFF_ATTEMPTS = 2


def code_change(
    ticket: JiraTicket,
    plan: Plan,
    repo_map: RepoMap,
    config: JarvisConfig,
    *,
    feedback: str = "",
) -> CodeChange:
    client = LiteLLMClient(config.litellm)
    checker = DiffQualityChecker(allowed_paths={fc.path for fc in plan.files_to_change})

    def read_original(path: str) -> str | None:
        return _read_original(repo_map.local_path, path)[0]

    user_prompt = _build_user_prompt(ticket, plan, repo_map, read_original)
    if feedback:
        user_prompt += f"\n\nThe previous attempt failed. Fix this:\n{feedback}"

    tokens_used = 0
    latency = 0.0
    issues: list[str] = []
    for _ in range(_MAX_DIFF_ATTEMPTS):
        prompt = user_prompt
        if issues:
            prompt += "\n\nYour previous diff was rejected:\n" + "\n".join(f"- {i}" for i in issues)

        result = client.complete(
            model_alias=config.models.coder,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        tokens_used += result.prompt_tokens + result.completion_tokens
        latency += result.latency_seconds

        report = checker.check(result.content, read_original)
        missing = check_missing_imports(report.files, repo_map, read_original) if report.ok else []
        if report.ok and not missing:
            tidied, style_notes = tidy_changed_files(report.files, read_original)
            for note in style_notes:
                logger.info("code style: %s", note)
            files = {
                path: _restore_style(text, *_read_original(repo_map.local_path, path)[1:])
                for path, text in tidied.items()
            }
            return CodeChange(
                files=files,
                diff=unified_diff(tidied, read_original) if style_notes else report.diff,
                files_changed=report.files_changed,
                tokens_used=tokens_used,
                latency_seconds=latency,
            )
        issues = report.issues or missing

    raise DiffQualityError(issues)


_ALWAYS_DEFINED = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__builtins__", "__spec__"}


def check_missing_imports(files: dict[str, str], repo_map: RepoMap, read_original) -> list[str]:
    """Names a changed Python file uses but never defines or imports (e.g. a new function
    called in the tests without importing it). Returns one feedback line per file.

    Checks the files as they will be after the change, not the diff: the stale import
    line is usually unchanged context, and the missing function is often new in this diff.
    Scope-insensitive on purpose: a name bound anywhere in the file counts as defined, so
    it never flags valid code, only names that exist nowhere in the file.
    """
    problems: list[str] = []
    for path, text in files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            problems.append(f"{path}: syntax error line {exc.lineno}: {exc.msg}")
            continue
        bound = _bound_names(tree)
        if "*" in bound:  # a star import could define anything: no reliable answer, don't guess
            continue
        undefined = sorted(_used_names(tree) - bound - _ALWAYS_DEFINED)
        if not undefined:
            continue
        hints = []
        for name in undefined:
            module = _module_defining(name, path, files, repo_map, read_original)
            hints.append(f"from {module} import {name}" if module else f"import {name}")
        problems.append(
            f"{path}: Import fehlt - {', '.join(undefined)} wird benutzt, aber nicht importiert/definiert. "
            f"Füge hinzu: {'; '.join(hints)}"
        )
    return problems


def _used_names(tree: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _bound_names(tree: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            bound.add(node.name)
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(tree)):
        bound.add("*")
    return bound


def _module_defining(name: str, current: str, files: dict[str, str], repo_map: RepoMap, read_original) -> str | None:
    """Dotted module (e.g. src.app) of a repo .py file that defines `name` at top level, after the change."""
    for path in sorted(set(files) | {p for p in repo_map.files if p.endswith(".py")}):
        if path == current or not path.endswith(".py"):
            continue
        text = files.get(path)
        if text is None:
            text = read_original(path)
        try:
            tree = ast.parse(text or "")
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                return path[:-3].replace("/", ".").removesuffix(".__init__")
    return None


def _build_user_prompt(ticket: JiraTicket, plan: Plan, repo_map: RepoMap, read_original) -> str:
    sections = []
    for fc in plan.files_to_change:
        content = read_original(fc.path)
        body = content if content is not None else "(new file - use --- /dev/null)"
        sections.append(f"--- {fc.path} ({fc.reason}) ---\n{body}")

    return (
        f"Ticket {ticket.key}: {ticket.summary}\n\n"
        f"Plan: {plan.approach}\n\n"
        f"Repository files ({len(repo_map.files)}):\n" + "\n".join(repo_map.files[:200])
        + "\n\nCurrent content of the files to change:\n\n" + "\n\n".join(sections)
        + "\n\nReturn one unified diff against these files (a/<path>, b/<path>) that touches only them."
    )


def _read_original(repo_root: Path, relative_path: str) -> tuple[str | None, bool, bool]:
    """Returns (clean text, had_bom, had_crlf). Text has no BOM and LF line endings."""
    path = repo_root / relative_path
    if not path.is_file():
        return None, False, False
    raw = path.read_bytes().decode("utf-8", errors="replace")
    had_bom = raw.startswith("﻿")
    text = raw.lstrip("﻿")
    had_crlf = "\r\n" in text
    return text.replace("\r\n", "\n"), had_bom, had_crlf


def _restore_style(text: str, had_bom: bool, had_crlf: bool) -> str:
    """Keep the file's original BOM/CRLF so the PR diff does not rewrite every line."""
    if had_crlf:
        text = text.replace("\n", "\r\n")
    return "﻿" + text if had_bom else text
