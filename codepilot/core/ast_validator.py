import ast
from typing import List, Set


# Builtins the control block is always allowed to use.
_SAFE_BUILTINS: Set[str] = {
    "print", "len", "range", "str", "int", "float", "bool",
    "list", "dict", "set", "tuple", "enumerate", "zip", "map",
    "filter", "sorted", "reversed", "sum", "min", "max", "abs",
    "round", "isinstance", "issubclass", "type", "repr", "hash",
    "id", "callable", "getattr", "hasattr", "vars", "dir",
    "Exception", "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "StopIteration",
    "True", "False", "None",
}

# Modules that are always importable regardless of allowed_imports config.
_ALWAYS_ALLOWED: Set[str] = {"tools"}


class SecurityViolation(Exception):
    pass


class ASTValidator(ast.NodeVisitor):
    """
    Validates a Python control block before exec().

    Checks:
      - Only whitelisted modules may be imported.
      - Direct calls to os.system / subprocess are blocked as a belt-and-suspenders
        guard (the sandbox globals don't expose them either, but belt-and-suspenders).
    """

    def __init__(self, allowed_imports: List[str]):
        self.allowed: Set[str] = _ALWAYS_ALLOWED | set(allowed_imports)

    def validate(self, code: str) -> ast.Module:
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            raise SecurityViolation(f"Syntax error in control block: {exc}") from exc
        self.visit(tree)
        return tree

    # ------------------------------------------------------------------
    # Visitors
    # ------------------------------------------------------------------

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            base = alias.name.split(".")[0]
            if base not in self.allowed:
                raise SecurityViolation(
                    f"Import of '{alias.name}' is not permitted. "
                    f"Add it to runtime.allowed_imports in your AgentFile."
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            base = node.module.split(".")[0]
            if base not in self.allowed:
                raise SecurityViolation(
                    f"Import from '{node.module}' is not permitted. "
                    f"Add it to runtime.allowed_imports in your AgentFile."
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        # Block direct access to dangerous builtins by name
        if isinstance(node.func, ast.Attribute):
            attr_chain = self._attr_chain(node.func)
            blocked = {"os.system", "os.popen", "subprocess.run",
                       "subprocess.Popen", "subprocess.call",
                       "subprocess.check_output"}
            if attr_chain in blocked:
                raise SecurityViolation(
                    f"Direct call to '{attr_chain}' is blocked. Use run_command() instead."
                )
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _attr_chain(node: ast.Attribute) -> str:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))
