"""Parse unified diffs and work out which Python functions a change can affect."""
from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BRANCH_NODES = (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp, ast.IfExp, ast.comprehension, ast.Match)


class PatchError(Exception):
    """The diff is malformed or does not apply to the files on disk."""


@dataclass
class Hunk:
    old_start: int
    old_len: int
    lines: list[str] = field(default_factory=list)  # each line keeps its ' ', '-' or '+' prefix


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None = None
    hunks: list[Hunk] = field(default_factory=list)
    added: set[int] = field(default_factory=set)  # line numbers in the new file
    removed: set[int] = field(default_factory=set)  # line numbers in the old file

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or "?"


@dataclass
class Param:
    name: str
    annotation: str | None
    default: str | None
    keyword_only: bool


@dataclass
class Function:
    name: str
    reason: str
    start: int
    end: int
    changed_lines: list[int]
    params: list[Param]
    numbers: list[str]  # numeric literals the function (or a module constant it reads) uses
    strings: list[str]
    lengths: list[int]  # integers compared against len(...)
    complexity: int
    old_code: str
    new_code: str


@dataclass
class FileContext:
    path: str
    old_source: str
    new_source: str
    lines_changed: int
    functions: list[Function] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _strip_prefix(raw: str) -> str | None:
    path = raw.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_ln = new_ln = old_left = new_left = 0
    for line in text.splitlines():
        if hunk is not None and (old_left > 0 or new_left > 0):
            tag = line[:1] or " "  # some editors strip the space off blank context lines
            if tag == "\\":
                continue
            if tag not in " -+":
                raise PatchError(f"{cur.path}: unexpected line inside hunk: {line!r}")
            hunk.lines.append(tag + line[1:])
            if tag == "-":
                cur.removed.add(old_ln)
                old_ln, old_left = old_ln + 1, old_left - 1
            elif tag == "+":
                cur.added.add(new_ln)
                new_ln, new_left = new_ln + 1, new_left - 1
            else:
                old_ln, new_ln, old_left, new_left = old_ln + 1, new_ln + 1, old_left - 1, new_left - 1
            continue
        if line.startswith("--- "):
            cur, hunk = FileDiff(_strip_prefix(line[4:])), None
            files.append(cur)
        elif line.startswith("+++ ") and cur is not None:
            cur.new_path = _strip_prefix(line[4:])
        elif (m := HUNK_RE.match(line)) and cur is not None:
            old_ln, old_left = int(m[1]), int(m[2] or 1)
            new_ln, new_left = int(m[3]), int(m[4] or 1)
            hunk = Hunk(old_ln, old_left)
            cur.hunks.append(hunk)
    return files


def apply_patch(old_source: str, diff: FileDiff) -> str:
    src = old_source.splitlines()
    out: list[str] = []
    i = 0
    for h in diff.hunks:
        start = h.old_start if h.old_len == 0 else h.old_start - 1  # "-N,0" inserts after line N
        if start < i:
            raise PatchError(f"{diff.path}: overlapping hunks at line {h.old_start}")
        out += src[i:start]
        i = start
        for line in h.lines:
            tag, body = line[0], line[1:]
            if tag in " -":
                if i >= len(src) or src[i] != body:
                    raise PatchError(f"{diff.path}: hunk at line {h.old_start} does not match line {i + 1} on disk")
                i += 1
            if tag in " +":
                out.append(body)
    out += src[i:]
    return "\n".join(out) + "\n" if out else ""


def load_patch(patch_file: str | Path, repo: str | Path) -> list[FileContext]:
    """Offline mode: old sources come from `repo`, new sources from applying the patch in memory."""
    repo = Path(repo)
    contexts = []
    for diff in parse_diff(Path(patch_file).read_text(encoding="utf-8")):
        old = (repo / diff.old_path).read_text(encoding="utf-8") if diff.old_path else ""
        new = apply_patch(old, diff) if diff.new_path else ""
        contexts.append(analyze(diff, old, new))
    return contexts


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def load_git(repo: str | Path, base: str, head: str = "HEAD") -> list[FileContext]:
    """Branch mode: diff `head` against its merge-base with `base`, as a pull request would."""
    repo = Path(repo)
    merge_base = _git(repo, "merge-base", base, head).strip()
    contexts = []
    for diff in parse_diff(_git(repo, "diff", "--no-color", "--no-ext-diff", merge_base, head)):
        old = _git(repo, "show", f"{merge_base}:{diff.old_path}") if diff.old_path else ""
        new = _git(repo, "show", f"{head}:{diff.new_path}") if diff.new_path else ""
        contexts.append(analyze(diff, old, new))
    return contexts


def _defs(tree: ast.Module) -> dict[str, ast.AST]:
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{sub.name}"] = sub
    return out


def _module_constants(tree: ast.Module) -> dict[str, ast.AST]:
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node
    return out


def _span(node: ast.AST) -> tuple[int, int]:
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]), node.end_lineno


def _hits(node: ast.AST, lines: set[int]) -> bool:
    start, end = _span(node)
    return any(start <= ln <= end for ln in lines)


def _names_used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _literals(nodes: list[ast.AST]) -> tuple[list[str], list[str]]:
    numbers, strings = [], []
    for root in nodes:
        for n in ast.walk(root):
            if not isinstance(n, ast.Constant) or isinstance(n.value, bool) or n.value is None:
                continue
            raw = n.value if isinstance(n.value, str) else repr(n.value)
            try:
                if not isinstance(n.value, (int, float, str)) or not Decimal(raw).is_finite():
                    continue
                numbers.append(raw)  # covers Decimal("50") style literals too
            except InvalidOperation:
                if isinstance(n.value, str) and len(n.value) <= 20:
                    strings.append(n.value)
    return list(dict.fromkeys(numbers)), list(dict.fromkeys(strings))


def _len_bounds(node: ast.AST) -> list[int]:
    out = []
    for cmp in (n for n in ast.walk(node) if isinstance(n, ast.Compare)):
        sides = [cmp.left, *cmp.comparators]
        if any(isinstance(s, ast.Call) and isinstance(s.func, ast.Name) and s.func.id == "len" for s in sides):
            out += [s.value for s in sides if isinstance(s, ast.Constant) and type(s.value) is int
                    and 0 <= s.value <= 10_000]
    return list(dict.fromkeys(out))


def _params(node: ast.FunctionDef) -> list[Param]:
    a = node.args
    positional = a.posonlyargs + a.args
    defaults = [None] * (len(positional) - len(a.defaults)) + a.defaults
    params = [Param(p.arg, p.annotation and ast.unparse(p.annotation), d and ast.unparse(d), False)
              for p, d in zip(positional, defaults)]
    params += [Param(p.arg, p.annotation and ast.unparse(p.annotation), d and ast.unparse(d), True)
               for p, d in zip(a.kwonlyargs, a.kw_defaults)]
    return params


def analyze(diff: FileDiff, old_source: str, new_source: str) -> FileContext:
    ctx = FileContext(diff.path, old_source, new_source, len(diff.added) + len(diff.removed))
    if not diff.path.endswith(".py"):
        ctx.skipped.append(f"`{diff.path}`: not a Python file")
        return ctx
    try:
        old_tree, new_tree = ast.parse(old_source), ast.parse(new_source)
    except SyntaxError as e:
        ctx.skipped.append(f"`{diff.path}`: could not parse ({e.msg}, line {e.lineno})")
        return ctx
    old_defs, new_defs = _defs(old_tree), _defs(new_tree)
    new_consts = _module_constants(new_tree)

    changed = {n for n, node in new_defs.items() if _hits(node, diff.added)}
    changed |= {n for n, node in old_defs.items() if _hits(node, diff.removed)}
    changed_consts = {n for n, node in new_consts.items() if _hits(node, diff.added)}
    changed_consts |= {n for n, node in _module_constants(old_tree).items() if _hits(node, diff.removed)}

    reasons: dict[str, str] = {n: "changed" for n in changed}
    for name, node in new_defs.items():
        if name in reasons:
            continue
        used = _names_used(node)
        calls = sorted(c for c in changed if c.split(".")[-1] in used)
        consts = sorted(changed_consts & used)
        if calls or consts:
            reasons[name] = "; ".join(filter(None, [calls and "calls " + ", ".join(calls),
                                                    consts and "reads " + ", ".join(consts)]))

    for name in sorted(reasons, key=lambda n: _span(new_defs[n])[0] if n in new_defs else 0):
        reason = reasons[name]
        if name not in new_defs:
            ctx.skipped.append(f"`{name}`: removed in this change")
        elif name not in old_defs:
            ctx.skipped.append(f"`{name}`: new function, no previous version to compare against")
        elif "." in name:
            ctx.skipped.append(f"`{name}`: method ({reason}); only module-level functions are exercised")
        elif isinstance(new_defs[name], ast.AsyncFunctionDef):
            ctx.skipped.append(f"`{name}`: async function ({reason}); not exercised")
        else:
            node, old_node = new_defs[name], old_defs[name]
            start, end = _span(node)
            lines = new_source.splitlines()
            doc = node.body[0] if ast.get_docstring(node) else None
            code_changes = sorted(ln for ln in diff.added if start <= ln <= end
                                  and not (doc and doc.lineno <= ln <= doc.end_lineno)
                                  and lines[ln - 1].strip() and not lines[ln - 1].strip().startswith("#"))
            const_nodes = [new_consts[c] for c in _names_used(node) & set(new_consts)]
            numbers, strings = _literals([old_node, node, *const_nodes])
            ctx.functions.append(Function(
                name=name, reason=reason, start=start, end=end,
                changed_lines=code_changes,
                params=_params(node), numbers=numbers, strings=strings,
                lengths=list(dict.fromkeys(_len_bounds(old_node) + _len_bounds(node))),
                complexity=1 + sum(isinstance(n, BRANCH_NODES) for n in ast.walk(node)),
                old_code=ast.get_source_segment(old_source, old_node) or "",
                new_code=ast.get_source_segment(new_source, node) or "",
            ))
    return ctx
