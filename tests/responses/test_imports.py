"""Receiver dependency hygiene: no agent, model, or tool imports."""

import ast
import pathlib


def _roots(package):
    root = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "backend" / package
    )
    roots = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("backend.")
            ):
                roots.add(node.module.split(".")[1])
    return roots


def test_responses_has_clean_dependencies():
    roots = _roots("responses")
    assert roots <= {
        "protocol", "identity", "persistence", "requests", "inbound",
    }, roots


def test_no_execution_or_tooling_imports():
    root = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "backend" / "responses"
    )
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = {node.module.split(".")[0]}
                if node.module.startswith("backend."):
                    modules.add(node.module.split(".")[1])
            else:
                continue
            for banned in (
                "agent", "mcp", "langgraph", "langchain", "gateway",
                "worker", "execution",
            ):
                assert banned not in modules, (path.name, banned)
