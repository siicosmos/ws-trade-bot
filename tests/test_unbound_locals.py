"""Catches the UnboundLocalError class of bugs before runtime.

A reader stay-up path crashed in production: a module-level
`_stayup_log_ts = 0.0` was shadowed by an assignment inside
main(), making the name local to the whole function - the
read executed before any assignment. This test flags any
local whose first read (source order) precedes its first
assignment.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)

# the reader is standalone by design; scan everything
TARGETS = [
    "reader/discord_reader.py",
    "run.py",
    "trader",
]


def _iter_python_files():
    for target in TARGETS:
        path = os.path.join(REPO, target)
        if path.endswith(".py"):
            yield path
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = [
                    d for d in dirs if d != "__pycache__"
                ]
                for f in files:
                    if f.endswith(".py"):
                        yield os.path.join(root, f)


def _function_locals_read_before_assignment(fn):
    """Names in fn that are assigned somewhere but whose first
    read (source order) comes before the first assignment."""
    assigned_linenos = {}
    comp_bound = set()   # names bound only by comprehension
    reads = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            # params and global/nonlocal names are pre-bound
            self.bound = set()

        def visit_FunctionDef(self, node):
            # nested functions have their own scope; ast.walk
            # analyzes them separately
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Global(self, node):
            self.bound.update(node.names)

        def visit_Nonlocal(self, node):
            self.bound.update(node.names)

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load):
                reads.append((node.lineno, node.id))
            elif isinstance(node.ctx, ast.Store):
                if node.id not in self.bound:
                    assigned_linenos.setdefault(
                        node.id, node.lineno
                    )
            self.generic_visit(node)

        def visit_ExceptHandler(self, node):
            if node.name:
                assigned_linenos.setdefault(node.name, node.lineno)
            self.generic_visit(node)

        def _comp(self, node):
            # comprehension targets bind before the element
            # expression is read, regardless of AST field order
            for gen in node.generators:
                for tgt in ast.walk(gen.target):
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id not in self.bound
                    ):
                        comp_bound.add(tgt.id)
            self.generic_visit(node)

        def visit_ListComp(self, node):
            self._comp(node)

        def visit_SetComp(self, node):
            self._comp(node)

        def visit_GeneratorExp(self, node):
            self._comp(node)

        def visit_DictComp(self, node):
            self._comp(node)

    # the function's own parameters are pre-bound; analyze the
    # body directly so nested defs are not folded in
    visitor = _Visitor()
    for arg in (
        fn.args.args + fn.args.posonlyargs + fn.args.kwonlyargs
    ):
        visitor.bound.add(arg.arg)
    if fn.args.vararg:
        visitor.bound.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        visitor.bound.add(fn.args.kwarg.arg)
    for stmt in fn.body:
        visitor.visit(stmt)

    # names bound only by comprehension targets are scoped to
    # the comprehension - they cannot be read "before" it in a
    # way that raises UnboundLocalError across statements
    assigned = set(assigned_linenos) - comp_bound
    problems = []
    for lineno, name in sorted(reads):
        if name in assigned and assigned_linenos[name] > lineno:
            problems.append(
                f"line {lineno}: local '{name}' read before its "
                f"first assignment (line {assigned_linenos[name]})"
            )
    return problems


def test_no_local_read_before_assignment():
    problems = []
    for path in _iter_python_files():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rel = os.path.relpath(path, REPO)
                for p in _function_locals_read_before_assignment(node):
                    problems.append(f"{rel}:{p}")
    assert not problems, "\n".join(problems)
