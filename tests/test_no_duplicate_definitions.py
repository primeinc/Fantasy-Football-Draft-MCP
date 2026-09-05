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

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ffdraft"


def _top_level_names(tree: ast.Module) -> dict[str, list[int]]:
    """Every name the module binds at module level, with the lines that bind it.

    Functions, classes and plain assignments only. A name rebound inside an
    `if`/`try` body is deliberate branching, not a merge artefact, so nested
    statements are not walked.
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
    return lines


def test_no_module_defines_the_same_top_level_name_twice():
    modules = sorted(SRC.glob("*.py"))
    # A glob that matched nothing would pass this test in silence, which is the
    # same failure the test exists to prevent, one level up.
    assert len(modules) > 5, f"expected the package, found {len(modules)} modules in {SRC}"

    findings = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lines in _top_level_names(tree).items():
            if len(lines) > 1:
                findings.append(f"{path.name}: {name} defined at lines "
                                + ", ".join(str(n) for n in lines))
    assert not findings, (
        "a module defines the same top-level name more than once; Python binds "
        "the last one and the earlier is dead code:\n  " + "\n  ".join(findings))
