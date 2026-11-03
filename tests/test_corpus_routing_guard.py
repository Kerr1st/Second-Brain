"""A legacy invoker import must not silently bypass corpus policy again."""
import ast
from pathlib import Path

from src.backends.base import assert_corpus_route
from src.backends.resolver import CORPUS_ROLES, load_profiles


ROOT = Path(__file__).resolve().parents[1]


def test_all_committed_profiles_obey_corpus_policy():
    profiles, default = load_profiles()
    assert default == "controlled"
    for profile in profiles.values():
        for role in CORPUS_ROLES:
            spec = profile[role]
            assert_corpus_route(spec.backend, spec.model, role)


def test_production_callers_do_not_import_or_construct_legacy_invokers():
    violations = []
    for directory in (ROOT / "src", ROOT / "scripts"):
        for path in directory.rglob("*.py"):
            if path.is_relative_to(ROOT / "src" / "backends"):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module in {"src.agent_invoker", "src.backends.kiro"}:
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    if any(alias.name in {"src.agent_invoker", "src.backends.kiro"} for alias in node.names):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                elif isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    if name in {"KiroInvoker", "AgentInvoker", "invoke"}:
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []
