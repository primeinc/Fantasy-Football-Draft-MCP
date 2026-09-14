"""`runner.record_tree` against real git: what the fixer's clone may and may not decide."""
import os
import shutil
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


def test_a_fixer_gitattributes_is_neither_applied_nor_recorded(repo, tmp_path):
    # git-lfs is configured as a required filter on this machine: applied, it would
    # store a pointer and put the real bytes in .git/lfs, where no review looks.
    main, clone, base = repo
    (clone / ".gitattributes").write_text("* filter=lfs diff=lfs merge=lfs -text\n")
    (clone / "payload.py").write_text("import os\n")
    # The control: the same add without --attr-source does route through lfs.
    control = subprocess.run(["git", f"--git-dir={main / '.git'}", f"--work-tree={clone}", "add",
                              "-A"], capture_output=True, text=True,
                             env=os.environ | {"GIT_INDEX_FILE": str(tmp_path / "control.index")})
    if not (main / ".git" / "lfs" / "objects").exists():
        pytest.skip(f"git-lfs did not run for the control add: {control.stderr.strip()}")
    shutil.rmtree(main / ".git" / "lfs")
    with pytest.raises(runner.StepRefused, match=r"\.gitattributes"):
        runner.record_tree(subprocess.run, clone, base, "queue/t", "m")
    assert "queue/t" not in git("branch", "--list", cwd=main)
    assert not (main / ".git" / "lfs").exists()


def test_an_lfs_filter_from_info_attributes_stores_the_real_bytes(repo, tmp_path):
    # --attr-source replaces only the tree's .gitattributes; .git/info/attributes
    # still applies, so the runner empties the lfs driver itself.
    main, clone, base = repo
    (main / ".git" / "info").mkdir(exist_ok=True)
    (main / ".git" / "info" / "attributes").write_text("* filter=lfs diff=lfs merge=lfs -text\n")
    (clone / "payload.py").write_bytes(b"import os\n")
    control = subprocess.run(["git", f"--attr-source={base}", f"--git-dir={main / '.git'}",
                              f"--work-tree={clone}", "add", "-A"], capture_output=True, text=True,
                             env=os.environ | {"GIT_INDEX_FILE": str(tmp_path / "control.index")})
    if not (main / ".git" / "lfs" / "objects").exists():
        pytest.skip(f"git-lfs did not run for the control add: {control.stderr.strip()}")
    shutil.rmtree(main / ".git" / "lfs")
    commit = runner.record_tree(subprocess.run, clone, base, "queue/t", "m")
    blob = subprocess.run(["git", "cat-file", "blob", f"{commit}:payload.py"], cwd=main,
                          capture_output=True, check=True).stdout
    assert blob == b"import os\n"
    assert not (main / ".git" / "lfs").exists()


@pytest.mark.parametrize("driver", ["custom", "x.y", "a=b"])
def test_any_configured_filter_driver_is_emptied(repo, tmp_path, driver):
    main, clone, base = repo
    script, marker = marker_script(tmp_path, "filter")
    git("config", f"filter.{driver}.clean", script.as_posix(), cwd=main)
    (main / ".git" / "info").mkdir(exist_ok=True)
    (main / ".git" / "info" / "attributes").write_text(f"*.py filter={driver}\n")
    (clone / "payload.py").write_bytes(b"import os\n")
    # The control: an ordinary add in this repository runs the clean filter.
    subprocess.run(["git", f"--git-dir={main / '.git'}", f"--work-tree={clone}", "add", "-A"],
                   capture_output=True, env=os.environ | {"GIT_INDEX_FILE": str(tmp_path / "c.index")})
    if not marker.exists():
        pytest.skip(f"gitattributes cannot name a driver {driver!r}: the control did not run it")
    marker.unlink()
    env = runner.no_filters(subprocess.run)
    keys = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert keys[f"filter.{driver}.clean"] == "" and keys[f"filter.{driver}.required"] == "false"
    commit = runner.record_tree(subprocess.run, clone, base, "queue/t", "m")
    assert not marker.exists()
    blob = subprocess.run(["git", "cat-file", "blob", f"{commit}:payload.py"], cwd=main,
                          capture_output=True, check=True).stdout
    assert blob == b"import os\n"


def test_a_nested_gitmodules_is_refused(repo):
    _main, clone, base = repo
    (clone / "sub").mkdir()
    (clone / "sub" / ".gitmodules").write_text("[submodule]\n")
    with pytest.raises(runner.StepRefused, match="sub/.gitmodules"):
        runner.record_tree(subprocess.run, clone, base, "queue/t", "m")


def marker_script(tmp_path, name):
    marker = tmp_path / f"{name}-ran"
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/bin/sh\necho ran >> '{marker.as_posix()}'\nexit 1\n")
    script.chmod(0o755)
    return script, marker


def test_a_configured_external_diff_does_not_run_in_the_fingerprint(repo, tmp_path, monkeypatch):
    main, _clone, _base = repo
    script, marker = marker_script(tmp_path, "diff-external")
    git("config", "diff.external", script.as_posix(), cwd=main)
    (main / "README").write_text("changed\n")
    # The control: an ordinary diff in this repository runs it.
    subprocess.run(["git", "diff", "HEAD"], cwd=main, capture_output=True)
    assert marker.exists()
    marker.unlink()
    monkeypatch.setattr(runner, "REPO", main)
    assert runner._git_fingerprint(subprocess.run, "queue/t")["diff"]
    assert not marker.exists()


def test_a_configured_signing_program_does_not_run_on_record(repo, tmp_path):
    main, clone, base = repo
    script, marker = marker_script(tmp_path, "gpg")
    git("config", "commit.gpgSign", "true", cwd=main)
    git("config", "gpg.program", script.as_posix(), cwd=main)
    # The control: an ordinary commit in this repository runs it. (commit-tree
    # alone does not read commit.gpgSign; --no-gpg-sign is kept for a config that
    # ever makes it.)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "m"], cwd=main, capture_output=True)
    assert marker.exists()
    marker.unlink()
    (clone / "new.py").write_text("x")
    assert runner.record_tree(subprocess.run, clone, base, "queue/t", "m")
    assert not marker.exists()


def test_config_files_follow_includes(repo, tmp_path, monkeypatch):
    main, _clone, _base = repo
    extra = tmp_path / "included.gitconfig"
    extra.write_text("[core]\n\tbare = false\n")
    git("config", "include.path", extra.as_posix(), cwd=main)
    monkeypatch.setattr(runner, "REPO", main)
    files = runner.config_files(subprocess.run)
    assert extra in files and main / ".git" / "config" in files


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
