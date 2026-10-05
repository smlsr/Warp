"""Scripts and hooks must parse on macOS system Python 3.9.

`X | Y` in a annotation is a SyntaxError on 3.9. `from __future__ import
annotations` postpones evaluation and does not change the parser. `match`,
`except*`, and `type` aliases are newer than 3.9 as well. Set unions such as
`SWITCHES | extra` are runtime expressions and stay.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [ROOT / "scripts", ROOT / "hooks"]


def _annotation_unions(tree: ast.AST):
    found = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.in_ann = 0

        def visit_AnnAssign(self, node):
            self.in_ann += 1
            self.visit(node.annotation)
            self.in_ann -= 1
            if node.value:
                self.visit(node.value)

        def visit_arg(self, node):
            if node.annotation:
                self.in_ann += 1
                self.visit(node.annotation)
                self.in_ann -= 1

        def visit_FunctionDef(self, node):
            self._fn(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def _fn(self, node):
            args = node.args
            for arg in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
                self.visit(arg)
            if args.vararg:
                self.visit(args.vararg)
            if args.kwarg:
                self.visit(args.kwarg)
            if node.returns:
                self.in_ann += 1
                self.visit(node.returns)
                self.in_ann -= 1
            for stmt in node.body:
                self.visit(stmt)

        def visit_BinOp(self, node):
            if self.in_ann and isinstance(node.op, ast.BitOr):
                found.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


class Python39SyntaxTests(unittest.TestCase):
    def test_scripts_and_hooks_avoid_3_10_syntax(self):
        problems = []
        for folder in TARGETS:
            for path in sorted(folder.glob("*.py")):
                tree = ast.parse(path.read_text(), filename=str(path))
                unions = _annotation_unions(tree)
                if unions:
                    problems.append(f"{path.relative_to(ROOT)} annotation | on lines {unions}")
                for node in ast.walk(tree):
                    if isinstance(node, ast.Match):
                        problems.append(f"{path.relative_to(ROOT)}:{node.lineno} match")
                    elif isinstance(node, ast.TryStar):
                        problems.append(f"{path.relative_to(ROOT)}:{node.lineno} except*")
                    elif isinstance(node, ast.TypeAlias):
                        problems.append(f"{path.relative_to(ROOT)}:{node.lineno} type alias")
                    elif isinstance(node, ast.JoinedStr):
                        for value in node.values:
                            if isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.JoinedStr):
                                problems.append(f"{path.relative_to(ROOT)}:{node.lineno} nested f-string")
        self.assertEqual(problems, [])
