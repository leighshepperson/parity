from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import promote_action_major as promoter
from scripts.promote_action_major import (
    PromotionDecision,
    PromotionError,
    ReleaseVersion,
    choose_promotion,
)

ROOT = Path(__file__).parents[1]
PROMOTION_SCRIPT = ROOT / "scripts" / "promote_action_major.py"


def run_git(directory: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def run_promotion(
    directory: Path, remote: Path, release_tag: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROMOTION_SCRIPT),
            "--release-tag",
            release_tag,
            "--remote",
            str(remote),
            *arguments,
        ],
        cwd=directory,
        check=False,
        capture_output=True,
        text=True,
    )


def write_release(repository: Path, version: str) -> str:
    version_file = repository / "src" / "parity" / "_version.py"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(
        f'"""Package version."""\n\n__version__ = "{version}"\n', encoding="utf-8"
    )
    run_git(repository, "add", str(version_file.relative_to(repository)))
    run_git(repository, "commit", "-m", f"release {version}")
    tag = f"v{version}"
    run_git(repository, "tag", tag)
    run_git(repository, "push", "origin", "HEAD:main", f"refs/tags/{tag}")
    return run_git(repository, "rev-parse", "HEAD")


def remote_tag(repository: Path, remote: Path, tag: str) -> str | None:
    output = run_git(repository, "ls-remote", "--refs", str(remote), f"refs/tags/{tag}")
    return output.split()[0] if output else None


@pytest.mark.parametrize(
    "tag",
    ["v0", "0.9.2", "v0.9", "v0.9.2rc1", "v00.9.2", "v0.09.2", "v0.9.02"],
)
def test_release_version_rejects_non_final_or_noncanonical_tags(tag: str) -> None:
    with pytest.raises(PromotionError, match=r"stable vMAJOR\.MINOR\.PATCH"):
        ReleaseVersion.from_tag(tag)


def test_promotion_decision_is_monotonic_and_idempotent() -> None:
    previous = ReleaseVersion.from_tag("v0.9.1")
    release = ReleaseVersion.from_tag("v0.9.2")
    future = ReleaseVersion.from_tag("v0.10.0")

    assert choose_promotion(release, "new", None, None) is PromotionDecision.CREATE
    assert choose_promotion(release, "new", previous, "old") is PromotionDecision.UPDATE
    assert choose_promotion(release, "new", release, "new") is PromotionDecision.NOOP
    with pytest.raises(PromotionError, match="different commit"):
        choose_promotion(release, "new", release, "other")
    with pytest.raises(PromotionError, match="refusing to roll"):
        choose_promotion(release, "new", future, "future")


def test_promotion_dry_run_create_update_and_rollback_guard(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    repository = tmp_path / "author"
    run_git(tmp_path, "init", "--bare", str(remote))
    run_git(tmp_path, "init", "--initial-branch=main", str(repository))
    run_git(repository, "config", "user.name", "Parity release test")
    run_git(repository, "config", "user.email", "parity-release@example.invalid")
    run_git(repository, "remote", "add", "origin", str(remote))

    first_commit = write_release(repository, "0.9.1")
    dry_run = run_promotion(repository, remote, "v0.9.1", "--dry-run")
    assert dry_run.returncode == 0, dry_run.stderr
    assert "would create v0" in dry_run.stdout
    assert remote_tag(repository, remote, "v0") is None

    bootstrap = run_promotion(repository, remote, "v0.9.1")
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert remote_tag(repository, remote, "v0") == first_commit

    second_commit = write_release(repository, "0.9.2")
    update = run_promotion(repository, remote, "v0.9.2")
    assert update.returncode == 0, update.stderr
    assert "Promoted v0 to v0.9.2" in update.stdout
    assert remote_tag(repository, remote, "v0") == second_commit

    repeated = run_promotion(repository, remote, "v0.9.2")
    assert repeated.returncode == 0, repeated.stderr
    assert "already points" in repeated.stdout

    rollback = run_promotion(repository, remote, "v0.9.1")
    assert rollback.returncode == 2
    assert "refusing to roll v0 back" in rollback.stderr
    assert remote_tag(repository, remote, "v0") == second_commit


def test_concurrent_major_tag_move_fails_the_force_with_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = tmp_path / "remote.git"
    repository = tmp_path / "author"
    run_git(tmp_path, "init", "--bare", str(remote))
    run_git(tmp_path, "init", "--initial-branch=main", str(repository))
    run_git(repository, "config", "user.name", "Parity release test")
    run_git(repository, "config", "user.email", "parity-release@example.invalid")
    run_git(repository, "remote", "add", "origin", str(remote))

    first_commit = write_release(repository, "0.9.1")
    assert run_promotion(repository, remote, "v0.9.1").returncode == 0
    second_commit = write_release(repository, "0.9.2")
    run_git(repository, "commit", "--allow-empty", "-m", "concurrent release writer")
    racing_commit = run_git(repository, "rev-parse", "HEAD")
    run_git(repository, "push", "origin", "HEAD:refs/heads/racing-writer")
    monkeypatch.chdir(repository)
    original_run_git = promoter.run_git
    raced = False

    def race_before_push(*arguments: str) -> str:
        nonlocal raced
        if arguments[0] == "push" and not raced:
            raced = True
            run_git(
                repository,
                f"--git-dir={remote}",
                "update-ref",
                "refs/tags/v0",
                racing_commit,
            )
        return original_run_git(*arguments)

    monkeypatch.setattr(promoter, "run_git", race_before_push)
    with pytest.raises(PromotionError, match="git push failed"):
        promoter.promote(release_tag="v0.9.2", remote=str(remote))
    assert raced is True
    assert remote_tag(repository, remote, "v0") == racing_commit
    assert remote_tag(repository, remote, "v0") != first_commit
    assert remote_tag(repository, remote, "v0") != second_commit


def test_current_user_guides_do_not_pin_parity_patch_versions() -> None:
    guides = [
        ROOT / "README.md",
        ROOT / "docs" / "GITHUB_ACTION.md",
        ROOT / "docs" / "USER_GUIDE.md",
    ]
    content = "\n".join(path.read_text(encoding="utf-8") for path in guides)
    assert re.search(r"leighshepperson/parity@v[0-9]+\.[0-9]+", content) is None
    assert re.search(r"parity-check\s*==\s*[0-9]", content) is None
    assert (ROOT / "README.md").read_text(encoding="utf-8").count("leighshepperson/parity@v0") == 1
    assert "leighshepperson/parity@v0" in (ROOT / "docs" / "GITHUB_ACTION.md").read_text(
        encoding="utf-8"
    )
    user_guide = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    assert "parity-check==" not in user_guide


def test_action_always_installs_its_own_revision() -> None:
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    action_guide = (ROOT / "docs" / "GITHUB_ACTION.md").read_text(encoding="utf-8")
    security_guide = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    assert "parity-version" not in action
    assert "parity-version" not in action_guide
    assert "parity-version" not in security_guide
    assert 'python -m pip install "$PARITY_ACTION_PATH"' in action
    assert 'python -m pip install "parity-check==' not in action
    assert "latest final 0.x release" in action_guide
    assert "minor release on this channel may change" in action_guide


def test_current_guides_describe_only_the_current_replay_contract() -> None:
    guides = [
        ROOT / "README.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "USER_GUIDE.md",
    ]
    content = "\n".join(path.read_text(encoding="utf-8") for path in guides).lower()
    for stale_phrase in (
        "legacy artifact",
        "legacy single",
        "older artifact",
        "reported as unverified",
        "replay contract 1",
        "replay contract 3",
        "manifest contract 1",
        "manifest contract 3",
    ):
        assert stale_phrase not in content
    architecture = guides[1].read_text(encoding="utf-8")
    assert "Manifest contract 2" in architecture
    assert "Replay contract 2" in architecture


def test_release_and_bootstrap_workflows_share_the_guarded_promoter() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    bootstrap = (ROOT / ".github" / "workflows" / "promote-action-major.yml").read_text(
        encoding="utf-8"
    )
    assert "needs: publish" in release
    assert "--skip-non-final" in release
    assert "python scripts/promote_action_major.py" in release
    assert "workflow_dispatch:" in bootstrap
    assert "push:" not in bootstrap
    assert "releases/latest" not in bootstrap
    assert "default: true" in bootstrap
    assert "python scripts/promote_action_major.py" in bootstrap
    assert "parity-action-major-${{ github.repository }}" in release
    assert "parity-action-major-${{ github.repository }}" in bootstrap
    assert re.search(r"actions/checkout@[0-9a-f]{40} # v", release) is not None
    assert re.search(r"actions/checkout@[0-9a-f]{40} # v", bootstrap) is not None


def test_build_gates_exclude_managed_candidate_checkouts() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"case_studies/**/candidate-src/**"' in project

    for workflow_name in ("ci.yml", "release.yml"):
        workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
        assert "case_studies/package-safety/candidate-src/upstream" in workflow
        assert '"/candidate-src/"' in workflow
