#!/usr/bin/env python3
"""Promote a stable release commit to its moving GitHub Action major tag."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

FINAL_TAG_PATTERN = re.compile(
    r"^v(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)$"
)
SOURCE_VERSION_PATTERN = re.compile(
    r'^__version__\s*=\s*["\'](?P<version>[0-9]+\.[0-9]+\.[0-9]+)["\']\s*$',
    re.MULTILINE,
)
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class PromotionError(RuntimeError):
    """Raised when promotion cannot proceed without risking an unsafe tag move."""


@dataclass(frozen=True, order=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def from_tag(cls, tag: str) -> ReleaseVersion:
        match = FINAL_TAG_PATTERN.fullmatch(tag)
        if match is None:
            raise PromotionError(f"release tag {tag!r} is not a stable vMAJOR.MINOR.PATCH tag")
        return cls(*(int(match[name]) for name in ("major", "minor", "patch")))

    @classmethod
    def from_source(cls, value: str) -> ReleaseVersion:
        try:
            major, minor, patch = value.split(".")
        except ValueError as exc:
            raise PromotionError(f"source version {value!r} is not MAJOR.MINOR.PATCH") from exc
        if not all(part.isdigit() for part in (major, minor, patch)):
            raise PromotionError(f"source version {value!r} is not MAJOR.MINOR.PATCH")
        return cls(int(major), int(minor), int(patch))

    @property
    def major_tag(self) -> str:
        return f"v{self.major}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class ObservedRef:
    oid: str
    commit: str


class PromotionDecision(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    NOOP = "no-op"


def run_git(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown git error").strip()
        raise PromotionError(f"git {arguments[0]} failed: {detail}") from exc
    return completed.stdout.strip()


def remote_oid(remote: str, ref: str) -> str | None:
    output = run_git("ls-remote", "--refs", remote, ref)
    if not output:
        return None
    rows = [line.split() for line in output.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref:
        raise PromotionError(f"remote returned an ambiguous value for {ref}")
    oid = rows[0][0]
    if OBJECT_ID_PATTERN.fullmatch(oid) is None:
        raise PromotionError(f"remote returned an invalid object ID for {ref}")
    return oid


def observe_remote_ref(remote: str, ref: str) -> ObservedRef | None:
    oid = remote_oid(remote, ref)
    if oid is None:
        return None
    run_git("fetch", "--quiet", "--no-tags", remote, ref)
    fetched_oid = run_git("rev-parse", "--verify", "FETCH_HEAD")
    if fetched_oid != oid:
        raise PromotionError(f"{ref} moved while it was being inspected; retry the promotion")
    commit = run_git("rev-parse", "--verify", "FETCH_HEAD^{commit}")
    return ObservedRef(oid=oid, commit=commit)


def source_version(commit: str) -> ReleaseVersion:
    source = run_git("show", f"{commit}:src/parity/_version.py")
    matches = list(SOURCE_VERSION_PATTERN.finditer(source))
    if len(matches) != 1:
        raise PromotionError(
            f"{commit} does not contain one plain __version__ assignment in src/parity/_version.py"
        )
    return ReleaseVersion.from_source(matches[0]["version"])


def choose_promotion(
    release_version: ReleaseVersion,
    release_commit: str,
    current_version: ReleaseVersion | None,
    current_commit: str | None,
) -> PromotionDecision:
    if (current_version is None) != (current_commit is None):
        raise PromotionError("current Action tag state is incomplete")
    if current_version is None:
        return PromotionDecision.CREATE
    if current_version.major != release_version.major:
        raise PromotionError(
            f"{release_version.major_tag} points at unexpected version {current_version}"
        )
    if release_version < current_version:
        raise PromotionError(
            f"refusing to roll {release_version.major_tag} back from {current_version} "
            f"to {release_version}"
        )
    if release_version == current_version:
        if release_commit != current_commit:
            raise PromotionError(
                f"version {release_version} exists at a different commit; refusing to rewrite it"
            )
        return PromotionDecision.NOOP
    return PromotionDecision.UPDATE


def promote(
    *,
    release_tag: str,
    remote: str = "origin",
    dry_run: bool = False,
    skip_non_final: bool = False,
) -> PromotionDecision | None:
    try:
        release_version = ReleaseVersion.from_tag(release_tag)
    except PromotionError:
        if skip_non_final:
            print(f"Skipping Action tag promotion for non-final release {release_tag!r}.")
            return None
        raise

    release_ref = f"refs/tags/{release_tag}"
    release = observe_remote_ref(remote, release_ref)
    if release is None:
        raise PromotionError(f"release ref {release_ref} does not exist on {remote}")
    observed_release_version = source_version(release.commit)
    if observed_release_version != release_version:
        raise PromotionError(
            f"{release_tag} contains package version {observed_release_version}, "
            f"expected {release_version}"
        )

    major_ref = f"refs/tags/{release_version.major_tag}"
    current = observe_remote_ref(remote, major_ref)
    current_version = source_version(current.commit) if current is not None else None
    decision = choose_promotion(
        release_version,
        release.commit,
        current_version,
        current.commit if current is not None else None,
    )
    if decision is PromotionDecision.NOOP:
        print(f"{release_version.major_tag} already points at {release_tag} ({release.commit}).")
        return decision
    if dry_run:
        print(
            f"Dry run: would {decision.value} {release_version.major_tag} at "
            f"{release_tag} ({release.commit})."
        )
        return decision

    if remote_oid(remote, release_ref) != release.oid:
        raise PromotionError(f"{release_ref} moved after validation; refusing promotion")
    expected_major_oid = current.oid if current is not None else ""
    if remote_oid(remote, major_ref) != (expected_major_oid or None):
        raise PromotionError(f"{major_ref} moved after validation; refusing promotion")

    run_git(
        "push",
        f"--force-with-lease={major_ref}:{expected_major_oid}",
        remote,
        f"{release.commit}:{major_ref}",
    )
    promoted_oid = remote_oid(remote, major_ref)
    if promoted_oid != release.commit:
        raise PromotionError(
            f"{major_ref} did not resolve to the validated release commit after promotion"
        )
    print(f"Promoted {release_version.major_tag} to {release_tag} ({release.commit}).")
    return decision


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", required=True, help="stable release tag to promote")
    parser.add_argument("--remote", default="origin", help="Git remote name or URL")
    parser.add_argument("--dry-run", action="store_true", help="validate without updating a tag")
    parser.add_argument(
        "--skip-non-final",
        action="store_true",
        help="exit successfully without promoting a prerelease or malformed tag",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        promote(
            release_tag=arguments.release_tag,
            remote=arguments.remote,
            dry_run=arguments.dry_run,
            skip_non_final=arguments.skip_non_final,
        )
    except PromotionError as exc:
        print(f"Action tag promotion failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
