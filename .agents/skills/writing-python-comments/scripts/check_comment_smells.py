#!/usr/bin/env python3
"""Report low-value Python comments AND missing comments at high-risk sites.

This scanner is intentionally asymmetric to match the writing-python-comments
skill: it flags both over-commenting (mechanical narration, vague helpers) and
under-commenting (public functions without docstrings, session boundaries
without intent comments, modules without docstrings).
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# --- Over-commenting patterns (line-level) ---

COMMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("narrates a basic action", re.compile(r"^\s*#\s*(get|set|create|update|delete)\b", re.I)),
    ("narrates a loop", re.compile(r"^\s*#\s*loop\s+through\b", re.I)),
    ("narrates a condition", re.compile(r"^\s*#\s*check\s+if\b", re.I)),
)

DOCSTRING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("vague helper docstring", re.compile(r"\bhelper\b", re.I)),
    ("mechanical function docstring", re.compile(r"^\s*this\s+function\b", re.I)),
)

VAGUE_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unsupported broad usage claim", re.compile(r"\bused\s+everywhere\b", re.I)),
    ("vague core-logic claim", re.compile(r"\bcore\s+logic\b", re.I)),
    ("vague helper claim", re.compile(r"\bimportant\s+helper\b", re.I)),
)

TRIPLE_QUOTE_RE = re.compile(r"(?P<prefix>[rubfRUBF]*)?(?P<quote>'''|\"\"\")")

# --- Under-commenting patterns (require comment within a few lines) ---

SESSION_ACQUIRE_RE = re.compile(r"\bacquire_(org|master)_async_session\s*\(")
SLOW_AWAIT_RE = re.compile(
    r"\bawait\s+(provider|llm|openai|anthropic|graph|client|http|requests|httpx|aiohttp)\b",
    re.I,
)
DUAL_WRITE_HINTS_RE = re.compile(r"\b(CANONICAL|LEGACY)_[A-Z_]+\b")

# Threshold below which a file is considered too small to require a module
# docstring. Keeps __init__.py and trivial utility files quiet.
MODULE_DOCSTRING_MIN_LOC = 50


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    reason: str
    text: str


def run_git_names(args: list[str]) -> set[Path]:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    return {Path(line.strip()) for line in result.stdout.splitlines() if line.strip().endswith(".py")}


def changed_python_files(repo_root: Path) -> list[Path]:
    names: set[Path] = set()
    names.update(run_git_names(["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD", "--", "*.py"]))
    names.update(run_git_names(["git", "diff", "--name-only", "--cached", "--diff-filter=ACMR", "--", "*.py"]))
    return sorted((repo_root / name for name in names if (repo_root / name).exists()))


def iter_docstring_spans(lines: list[str]) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    in_docstring = False
    start_line = 0
    quote = ""
    buffer: list[str] = []

    for index, line in enumerate(lines, start=1):
        if not in_docstring:
            match = TRIPLE_QUOTE_RE.search(line)
            if not match:
                continue
            quote = match.group("quote")
            start_line = index
            after = line[match.end() :]
            if quote in after:
                end_index = after.find(quote)
                spans.append((start_line, index, after[:end_index].strip()))
                quote = ""
                continue
            in_docstring = True
            buffer = [after.strip()]
            continue

        if quote in line:
            before = line.split(quote, 1)[0]
            buffer.append(before.strip())
            spans.append((start_line, index, " ".join(part for part in buffer if part).strip()))
            in_docstring = False
            quote = ""
            buffer = []
        else:
            buffer.append(line.strip())

    return spans


def has_comment_within(lines: list[str], target_line: int, before: int = 5, after: int = 0) -> bool:
    """True if any line within the window around target_line is a `#` comment."""
    start = max(0, target_line - 1 - before)
    end = min(len(lines), target_line + after)
    for line in lines[start:end]:
        if line.lstrip().startswith("#"):
            return True
    return False


def scan_overcommenting(path: Path, lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        for reason, pattern in COMMENT_PATTERNS + VAGUE_PHRASES:
            if pattern.search(line):
                findings.append(Finding(path, line_number, reason, stripped))
                break

    for start_line, _end_line, text in iter_docstring_spans(lines):
        if not text:
            continue
        for reason, pattern in DOCSTRING_PATTERNS + VAGUE_PHRASES:
            if pattern.search(text):
                preview = text if len(text) <= 120 else f"{text[:117]}..."
                findings.append(Finding(path, start_line, reason, preview))
                break

    return findings


def scan_undercommenting_lines(path: Path, lines: list[str]) -> list[Finding]:
    """Line-based heuristics that don't require a parseable AST."""
    findings: list[Finding] = []

    # Session boundary: acquire_*_async_session followed by a slow await within
    # ~20 lines, with no comment in the gap.
    for index, line in enumerate(lines, start=1):
        if not SESSION_ACQUIRE_RE.search(line):
            continue
        window = lines[index : index + 20]
        slow_await_offset = None
        for offset, candidate in enumerate(window, start=1):
            if SLOW_AWAIT_RE.search(candidate):
                slow_await_offset = offset
                break
        if slow_await_offset is None:
            continue
        if has_comment_within(lines, index, before=5, after=slow_await_offset):
            continue
        findings.append(
            Finding(
                path,
                index,
                "session boundary near slow await without intent comment",
                line.strip(),
            )
        )

    # Dual-write of CANONICAL_* and LEGACY_* keys without a nearby comment.
    seen_canonical = None
    for index, line in enumerate(lines, start=1):
        if not DUAL_WRITE_HINTS_RE.search(line):
            continue
        if "CANONICAL" in line:
            seen_canonical = index
            continue
        if "LEGACY" in line and seen_canonical is not None and index - seen_canonical <= 5:
            if not has_comment_within(lines, seen_canonical, before=5, after=index - seen_canonical):
                findings.append(
                    Finding(
                        path,
                        seen_canonical,
                        "dual canonical/legacy write without migration comment",
                        line.strip(),
                    )
                )
            seen_canonical = None

    return findings


def scan_undercommenting_ast(path: Path, source: str, lines: list[str]) -> list[Finding]:
    """AST-based checks: module docstring, public function docstrings."""
    findings: list[Finding] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return findings

    # Module docstring check. Skip __init__.py and very small files.
    if path.name != "__init__.py" and len(lines) >= MODULE_DOCSTRING_MIN_LOC:
        if ast.get_docstring(tree) is None:
            findings.append(
                Finding(
                    path,
                    1,
                    f"module > {MODULE_DOCSTRING_MIN_LOC} LOC missing module docstring",
                    "(top of file)",
                )
            )

    # Public function/method docstring check.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if name.startswith("_"):
                continue
            if name in {"main"}:
                continue
            if ast.get_docstring(node) is None:
                findings.append(
                    Finding(
                        path,
                        node.lineno,
                        "public function/method missing docstring",
                        f"def {name}(...)",
                    )
                )
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            if ast.get_docstring(node) is None:
                findings.append(
                    Finding(
                        path,
                        node.lineno,
                        "public class missing docstring",
                        f"class {node.name}",
                    )
                )

    return findings


def scan_file(path: Path) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        source = path.read_text()
    lines = source.splitlines()

    findings: list[Finding] = []
    findings.extend(scan_overcommenting(path, lines))
    findings.extend(scan_undercommenting_lines(path, lines))
    findings.extend(scan_undercommenting_ast(path, source, lines))
    return findings


def resolve_files(args: argparse.Namespace) -> list[Path]:
    repo_root = Path.cwd()
    if args.files:
        return [Path(file).resolve() for file in args.files if Path(file).suffix == ".py" and Path(file).exists()]
    return changed_python_files(repo_root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="Optional Python files to scan. Defaults to changed .py files.")
    args = parser.parse_args()

    files = resolve_files(args)
    if not files:
        print("No changed Python files found to scan.")
        return 0

    findings: list[Finding] = []
    for path in files:
        findings.extend(scan_file(path))

    if not findings:
        print(f"No comment smells or missing-comment sites found in {len(files)} Python file(s).")
        return 0

    print(f"Found {len(findings)} comment finding(s) across {len(files)} file(s):")
    for finding in findings:
        try:
            display_path = finding.path.relative_to(Path.cwd())
        except ValueError:
            display_path = finding.path
        print(f"{display_path}:{finding.line_number}: {finding.reason}: {finding.text}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
