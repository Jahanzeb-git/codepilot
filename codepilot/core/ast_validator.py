"""
File: ast_validator.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-04-16

Description: 
CodePilot Abstract Syntax Tree (AST) Validator.
Acts as the first line of defense for the agentic execution runtime.

Architectural Notes:
Originally designed to strictly block dangerous imports and system calls, this 
module now facilitates an open-sandbox architecture. We allow complete Python freedom 
(all imports and OS calls) under the assumption that CodePilot runs inside a Docker 
container, treating the container shell as the true security boundary.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

import ast
from typing import List, Set


class SecurityViolation(Exception):
    pass


class ASTValidator(ast.NodeVisitor):
    """
    Validates a Python control block before exec().

    In containerized environments, we allow all imports and arbitrary Python logic.
    We only check for basic syntax validity. Destructive host file operations
    are guarded dynamically at runtime using path constraints.
    """

    def __init__(self, allowed_imports: List[str] = None):
        # We ignore allowed_imports now as the container is the security boundary
        pass

    def validate(self, code: str) -> ast.Module:
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            raise SecurityViolation(f"Syntax error in control block: {exc}") from exc
        self.visit(tree)
        return tree
