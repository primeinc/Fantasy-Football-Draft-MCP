"""`runner.record_tree` against real git: what the fixer's clone may and may not decide."""
import subprocess

import pytest

from ffdraft import runner

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t"}


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in IDENTITY.items():
        monkeypatch.setenv(k, v)
    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", cwd=main)
    (main / "README").write_text("base\n")
    git("add", "README", cwd=main)
    git("commit", "-q", "-m", "base", cwd=main)
    monkeypatch.setattr(runner, "GIT_DIR", main / ".git")
    clone = tmp_path / "trees" / "fix"
    clone.mkdir(parents=True)
    (clone / "README").write_text("base\n")
    return main, clone, git("rev-parse", "HEAD", cwd=main)


def test_the_fixers_files_are_stored_as_written(repo):
    # LF content: core.autocrlf=input (~/.gitconfig on this machine) converts CRLF
    # on add, as it does for any add in the main checkout.
    main, clone, base = repo
    (clone / "new.py").write_bytes(b"print('x')\n")
    commit = runner.record_tree(subprocess.run, clone, base, "queue/t", "m")
    assert commit and git("rev-parse", "refs/heads/queue/t", cwd=main) == commit
    shown = subprocess.run(["git", "cat-file", "blob", f"{commit}:new.py"], cwd=main,
                           capture_output=True, check=True).stdout
    assert shown == b"print('x')\n"


def test_an_unchanged_tree_records_nothing(repo):
    main, clone, base = repo
    assert runner.record_tree(subprocess.run, clone, base, "queue/t", "m") is None
    assert "queue/t" not in git("branch", "--list", cwd=main)


def test_an_existing_branch_is_never_moved(repo):
    main, clone, base = repo
    git("branch", "queue/t", cwd=main)
    (clone / "new.py").write_text("x")
    with pytest.raises(subprocess.CalledProcessError):
        runner.record_tree(subprocess.run, clone, base, "queue/t", "m")
    assert git("rev-parse", "queue/t", cwd=main) == base


def test_a_fixer_gitattributes_is_neither_applied_nor_recorded(repo):
    # git-lfs is configured as a required filter on this machine: applied, it would
    # store a pointer and put the real bytes in .git/lfs, where no review looks.
    main, clone, base = repo
    (clone / ".gitattributes").write_text("* filter=lfs diff=lfs merge=lfs -text\n")
    (clone / "payload.py").write_text("import os\n")
    with pytest.raises(runner.StepRefused, match=r"\.gitattributes"):
        runner.record_tree(subprocess.run, clone, base, "queue/t", "m")
    assert "queue/t" not in git("branch", "--list", cwd=main)
    assert not (main / ".git" / "lfs").exists()


def test_a_nested_gitmodules_is_refused(repo):
    _main, clone, base = repo
    (clone / "sub").mkdir()
    (clone / "sub" / ".gitmodules").write_text("[submodule]\n")
    with pytest.raises(runner.StepRefused, match="sub/.gitmodules"):
        runner.record_tree(subprocess.run, clone, base, "queue/t", "m")


def test_a_planted_hooks_directory_does_not_run(repo, tmp_path):
    main, clone, base = repo
    hooks = tmp_path / "planted-hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker.as_posix()}'\n")
    hook.chmod(0o755)
    git("config", "core.hooksPath", str(hooks), cwd=main)
    # The control: an ordinary ref update in this repository runs the hook.
    git("update-ref", "refs/heads/control", base, cwd=main)
    assert marker.exists()
    marker.unlink()
    (clone / "new.py").write_text("x")
    assert runner.record_tree(subprocess.run, clone, base, "queue/t", "m")
    assert not marker.exists()
