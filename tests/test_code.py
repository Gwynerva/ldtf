"""Static sanity of the package without third-party linters: every name a module reads is bound somewhere in it
(a missing import on a rarely used page otherwise shows up only as a 500 for the user), no import is unused, and no
string has an invalid escape like "\\p" (a SyntaxWarning today, an error in a later Python)."""

import ast
import builtins
import sys
import unittest
import warnings
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "dtf_backup"


def problems(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            tree = ast.parse(src, path.name)
    except SyntaxError as e:
        return [f"{path.name}:{e.lineno}: {e.msg}"]
    lines = src.splitlines()
    imported: dict[str, int] = {}
    bound = set(dir(builtins)) | {"__file__", "__name__"}
    used: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported[(a.asname or a.name).split(".")[0]] = n.lineno
        elif isinstance(n, ast.ImportFrom) and n.module != "__future__":
            for a in n.names:
                imported[a.asname or a.name] = n.lineno
        elif isinstance(n, ast.Name):
            (used if isinstance(n.ctx, ast.Load) else bound).add(n.id)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            used.add(n.value)   # string annotations: "App" for a TYPE_CHECKING import
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
    out = [f"{path.name}:{ln}: unused import {name}" for name, ln in imported.items()
           if name not in used and "noqa" not in lines[ln - 1] and path.name != "__init__.py"]
    read = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    out += [f"{path.name}: undefined name {name}" for name in sorted(read - bound - set(imported))]
    return out


class NamesTest(unittest.TestCase):
    def test_names(self) -> None:
        found = [p for f in sorted(PKG.rglob("*.py")) if "__pycache__" not in f.parts for p in problems(f)]
        self.assertEqual(found, [], "\n".join(found))


if __name__ == "__main__":
    sys.exit(unittest.main())
