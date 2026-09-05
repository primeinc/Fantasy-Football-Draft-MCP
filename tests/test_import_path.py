"""The suite must load the checkout it was collected from.

A git worktree has no `.venv` of its own, so `just check` derives one: this
checkout's if it has one, else the main checkout's. That borrowed venv installs
the package editable, and the resulting `.pth` names whichever checkout built
it — so without help, pytest imports the *other* tree while ruff and ty check
this one, because those two are handed paths on the command line. The run comes
back green for code it never loaded, and nothing either tool prints says so.

`PYTHONPATH` fixes it, and precedence over site-packages is why it works. That
is an argument rather than an observation, and it holds until a `.pth` does
something unusual. This asserts it instead.
"""
import pathlib

import ffdraft

TESTS_ROOT = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parent


def test_the_imported_package_lives_in_the_checkout_under_test():
    imported = pathlib.Path(ffdraft.__file__).resolve()
    expected = REPO_ROOT / "src" / "ffdraft" / "__init__.py"
    assert imported == expected, (
        f"pytest collected {TESTS_ROOT} but imported ffdraft from {imported}.\n"
        f"Expected {expected}.\n"
        "The suite is testing a different checkout than the one it was collected "
        "from — most likely a worktree borrowing another checkout's .venv without "
        "PYTHONPATH. Every result in this run is about the other tree. Run "
        "`just check` (which exports PYTHONPATH), or `just setup` to build a "
        "local venv."
    )


def test_every_ffdraft_module_already_loaded_comes_from_that_same_checkout():
    """One module resolving correctly does not settle the rest.

    `__init__` can come from one tree while a submodule is picked up from
    another: a stale `.pth`, a leftover build directory, or an `src` shadowed on
    `sys.path` all split the package across roots, and the split is invisible
    until two modules disagree about a constant.
    """
    import sys

    src = (REPO_ROOT / "src").resolve()
    stray = []
    for name, module in sorted(sys.modules.items()):
        if not (name == "ffdraft" or name.startswith("ffdraft.")):
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:  # namespace packages carry no file
            continue
        path = pathlib.Path(origin).resolve()
        if src not in path.parents:
            stray.append(f"{name} -> {path}")
    assert not stray, (
        "these ffdraft modules were imported from outside "
        f"{src}:\n  " + "\n  ".join(stray)
    )
