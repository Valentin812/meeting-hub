import ast
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Hygiene(unittest.TestCase):
    def test_no_duplicate_methods(self):
        """Второе определение метода молча заменяет первое (так «проигрыватель» долго открывал Windows)."""
        problems = []
        for path in glob.glob(os.path.join(ROOT, "*.py")):
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                seen = {}
                for fn in node.body:
                    if isinstance(fn, ast.FunctionDef) and not fn.decorator_list:
                        if fn.name in seen:
                            problems.append(f"{os.path.basename(path)}:{node.name}.{fn.name} "
                                            f"(строки {seen[fn.name]} и {fn.lineno})")
                        seen[fn.name] = fn.lineno
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
