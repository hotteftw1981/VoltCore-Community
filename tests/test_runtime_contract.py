"""Keep required shared runtime APIs present when edition-specific code is removed."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeContractTests(unittest.TestCase):
    def test_every_cross_module_db_reference_exists(self):
        tree = ast.parse((ROOT / "app/db.py").read_text(encoding="utf-8"))
        symbols = {node.name for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                symbols.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                symbols.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        missing = []
        for path in sorted((ROOT / "app").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "db" and node.attr not in symbols):
                    missing.append(f"{path.name}:{node.lineno}: db.{node.attr}")
        self.assertEqual([], missing, "Shared DB API removed: " + ", ".join(missing))

    def test_first_run_audit_uses_current_limit_variable(self):
        tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
        functions = [node for node in tree.body
                     if isinstance(node, ast.AsyncFunctionDef) and node.name == "first_run_submit"]
        self.assertEqual(1, len(functions))
        names = {node.id for node in ast.walk(functions[0]) if isinstance(node, ast.Name)}
        self.assertNotIn("credit_enabled", names)
        self.assertIn("default_limit_enabled", names)


if __name__ == "__main__":
    unittest.main()
