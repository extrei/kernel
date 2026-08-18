from __future__ import annotations

import ast
from pathlib import Path
import unittest


class RunnerBoundaryTests(unittest.TestCase):
    def test_runner_imports_only_public_names_from_kernel(self) -> None:
        runner_root = Path(__file__).parents[1] / "runner"
        violations: list[str] = []

        for path in sorted(runner_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("kernel."):
                        violations.append(
                            f"{path.name}:{node.lineno} imports internal module "
                            f"{node.module}"
                        )
                    elif node.module == "kernel":
                        for imported in node.names:
                            if imported.name.startswith("_"):
                                violations.append(
                                    f"{path.name}:{node.lineno} imports "
                                    f"private name {imported.name}"
                                )
                elif isinstance(node, ast.Import):
                    for imported in node.names:
                        if imported.name.startswith("kernel."):
                            violations.append(
                                f"{path.name}:{node.lineno} imports internal module "
                                f"{imported.name}"
                            )

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
