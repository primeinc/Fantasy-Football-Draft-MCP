"""No module may define the same top-level name twice.

Two agents fixing one defect on separate branches produce two definitions of the
same function. Git merges them as independent additions at different offsets, so
no conflict is raised; Python then binds the last one and the earlier one is
dead. Nothing in ruff, ty or pytest reports it.

That is not hypothetical here. It happened twice in one integration:

  * `model._discount` arrived from two branches. The merge kept both. The
    surviving definition was the earlier UNBOUNDED form (`v / m`), while the
    bounded reflection that replaced it sat dead above. The unbounded form
    inflates a negative pick_value by up to fiftyfold, and `replay` sums
    pick_regret per team, so one backup quarterback scored his team 8641
    against 398 for the next worst. The merge would have shipped the exact bug
    the merge existed to deliver the fix for.

  * `model.DISCOUNT_CEILING` arrived twice the same way one merge later. Both
    were 2.0, so it was harmless, which is the point: the mechanism does not
    care whether the duplicate is dangerous.

Both were found by hand. This is the durable form of that check.
"""
import ast
import collections
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Both trees. A duplicated test function is the same mechanism with the same
# silence: pytest runs the last one and the earlier is dead. This integration
# merged test files from three branches, so tests/ is exactly as exposed as src.
ROOTS = (ROOT / "src" / "ffdraft", ROOT / "tests")


def _top_level_names(tree: ast.Module) -> dict[str, list[int]]:
    """Every name the module binds at module level, with the lines that bind it.

    Functions, classes, plain assignments and imports. A name rebound inside an
    `if`/`try` body is deliberate branching, not a merge artefact, so nested
    statements are not walked — which is also why imports can be included for
    free: the `try/except ImportError` SDK fallback in `server.py` binds
    `Context` and `_Server` twice, and both bindings live inside the `try`,
    where this walk never looks.
    """
    lines: dict[str, list[int]] = collections.defaultdict(list)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lines[node.name].append(node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    lines[target.id].append(node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            lines[node.target.id].append(node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # `import a.b` binds `a`; an alias binds the alias. A later `def
            # foo` shadowing `from .x import foo` is a real merge artefact.
            for alias in node.names:
                if alias.name == "*":
                    continue
                lines[alias.asname or alias.name.split(".")[0]].append(node.lineno)
    return lines


def test_no_module_defines_the_same_top_level_name_twice():
    # rglob, not glob: a subpackage added later would otherwise be silently
    # uncovered, which is the failure this test exists to prevent, one level up.
    modules = sorted(p for root in ROOTS for p in root.rglob("*.py"))
    assert len(modules) > 25, f"expected both trees, found {len(modules)} modules"

    findings = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lines in _top_level_names(tree).items():
            if len(lines) > 1:
                findings.append(f"{path.relative_to(ROOT).as_posix()}: {name} defined at lines "
                                + ", ".join(str(n) for n in lines))
    assert not findings, (
        "a module defines the same top-level name more than once; Python binds "
        "the last one and the earlier is dead code:\n  " + "\n  ".join(findings))
