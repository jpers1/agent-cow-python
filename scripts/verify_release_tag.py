#!/usr/bin/env python3
"""Verify that a requested release tag matches source and merged main."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPOSITORY_ROOT / "agentcow" / "__init__.py"
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def package_version() -> str:
    match = VERSION_PATTERN.search(VERSION_FILE.read_text())
    if match is None:
        raise ValueError(f"cannot read __version__ from {VERSION_FILE}")
    return match.group(1)


def validate_tag_name(tag: str, version: str) -> None:
    if not tag.startswith("v"):
        raise ValueError("release tag must start with 'v'")
    expected = f"v{version}"
    if tag != expected:
        raise ValueError(f"release tag {tag!r} does not match package tag {expected!r}")


def git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def verify_checked_out_tag(tag: str, main_ref: str) -> None:
    version = package_version()
    validate_tag_name(tag, version)

    tag_commit = git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    head_commit = git("rev-parse", "HEAD")
    if tag_commit != head_commit:
        raise ValueError(
            f"checked-out commit {head_commit} is not requested tag commit {tag_commit}"
        )

    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag_commit, main_ref],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"tag commit {tag_commit} is not reachable from {main_ref}")

    print(
        f"PASS: {tag} matches package version {version}, resolves to HEAD, "
        f"and is reachable from {main_ref}"
    )


def run_self_test() -> None:
    version = package_version()
    validate_tag_name(f"v{version}", version)

    invalid_tags = (version, "v0.0.0-invalid")
    for invalid_tag in invalid_tags:
        try:
            validate_tag_name(invalid_tag, version)
        except ValueError:
            continue
        raise AssertionError(f"invalid release tag was accepted: {invalid_tag}")

    print(f"PASS: release-tag validation accepts only v{version}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", nargs="?")
    parser.add_argument("--main-ref", default="origin/main")
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args()

    if arguments.self_test:
        if arguments.tag is not None:
            parser.error("tag cannot be combined with --self-test")
        run_self_test()
        return 0

    if arguments.tag is None:
        parser.error("tag is required unless --self-test is used")
    verify_checked_out_tag(arguments.tag, arguments.main_ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
