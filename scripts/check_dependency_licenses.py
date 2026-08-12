#!/usr/bin/env python3
"""Inventory locked runtime dependency licenses and enforce manual-review policy."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_dependencies(pyproject: Path) -> list[dict[str, str]]:
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    pending = [(Requirement(item), False) for item in project["dependencies"]]
    packages: dict[str, Any] = {}
    extras: dict[str, set[str]] = {}
    while pending:
        requirement, marker_evaluated = pending.pop()
        if (
            not marker_evaluated
            and requirement.marker
            and not requirement.marker.evaluate({"extra": ""})
        ):
            continue
        name = normalized(requirement.name)
        new_extras = set(requirement.extras) - extras.get(name, set())
        if name in packages and not new_extras:
            continue
        try:
            package = packages.setdefault(name, distribution(requirement.name))
        except PackageNotFoundError as error:
            raise RuntimeError(f"runtime dependency is not installed: {requirement.name}") from error
        extras.setdefault(name, set()).update(requirement.extras)
        contexts = ["", *sorted(extras[name])]
        for item in package.requires or []:
            child = Requirement(item)
            if child.marker and not any(
                child.marker.evaluate({"extra": extra}) for extra in contexts
            ):
                continue
            pending.append((child, True))

    result = []
    for name, package in sorted(packages.items()):
        metadata = package.metadata
        classifiers = [
            item
            for item in metadata.get_all("Classifier", [])
            if item.startswith("License ::")
        ]
        license_text = (
            metadata.get("License-Expression")
            or metadata.get("License")
            or "; ".join(classifiers)
            or "UNKNOWN"
        )
        result.append({"name": name, "version": package.version, "license": license_text})
    return result


def check(
    *,
    pyproject: Path,
    policy_path: Path,
    output: Path | None,
    release: bool,
    attestation: str | None,
) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    packages = runtime_dependencies(pyproject)
    by_name = {item["name"]: item for item in packages}
    errors: list[str] = []
    reviews: list[dict[str, str]] = []
    review_triggers = tuple(item.upper() for item in policy["review_triggers"])
    required = {
        item["name"]
        for item in packages
        if any(trigger in item["license"].upper() for trigger in review_triggers)
    }
    declared = {normalized(name) for name in policy["review_required"]}
    for name in sorted(required - declared):
        errors.append(f"review-triggering dependency is not declared in policy: {name}")
    for name, rule in policy["review_required"].items():
        package = by_name.get(normalized(name))
        if package is None:
            errors.append(f"policy dependency is absent: {name}")
            continue
        if package["version"] != rule["locked_version"]:
            errors.append(
                f"{name} version changed: {package['version']} != {rule['locked_version']}"
            )
        for fragment in rule["expected_license_contains"]:
            if fragment not in package["license"]:
                errors.append(f"{name} license metadata no longer contains: {fragment}")
        reviews.append(package)
        required_attestation = rule.get("release_attestation")
        if release and required_attestation and attestation != required_attestation:
            errors.append(f"release approval is missing or invalid for {name}")

    report = {
        "schema": "cardrag.dependency-license-inventory.v1",
        "project_license": policy["project_license"],
        "mode": "release" if release else "inventory",
        "packages": packages,
        "manual_review_required": reviews,
        "status": "failed" if errors else "passed",
        "errors": errors,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--policy", type=Path, default=Path("legal/dependency-license-policy.json")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--attestation")
    args = parser.parse_args()
    report = check(
        pyproject=args.pyproject,
        policy_path=args.policy,
        output=args.output,
        release=args.release,
        attestation=args.attestation,
    )
    if report["status"] != "passed":
        for error in report["errors"]:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
