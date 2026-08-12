#!/usr/bin/env python3
"""Inventory runtime licenses and preserve exact binary-license payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, distribution, distributions
from pathlib import Path, PurePosixPath
from typing import Any

_RELEASE_DECISION = "include-with-recorded-notice-and-compliance-obligations"
_RELEASE_REVIEW_SEMANTICS = (
    "Records the notice and compliance obligations accepted for packaging; "
    "it is not legal advice or a legal-license approval."
)
_RELEASE_PAYLOAD_SOURCE = "installed-dist-info/licenses"


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _package_info(name: str) -> tuple[Any, dict[str, str]]:
    try:
        package = distribution(name)
    except PackageNotFoundError as error:
        raise RuntimeError(f"runtime dependency is not installed: {name}") from error
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
    return package, {"name": normalized(name), "version": package.version, "license": license_text}


def runtime_dependencies(pyproject: Path) -> list[dict[str, str]]:
    # ``packaging`` is only a development/check dependency. Keep the import lazy
    # so the production-image build can run ``--notices-only`` using stdlib.
    from packaging.requirements import Requirement

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
        _, info = _package_info(name)
        info["version"] = package.version
        result.append(info)
    return result


def installed_dependencies() -> list[dict[str, str]]:
    """Return the exact set installed in the active runtime environment."""
    result: dict[str, dict[str, str]] = {}
    for package in distributions():
        raw_name = package.metadata.get("Name")
        if not raw_name:
            continue
        name = normalized(raw_name)
        metadata = package.metadata
        classifiers = [
            item
            for item in metadata.get_all("Classifier", [])
            if item.startswith("License ::")
        ]
        result[name] = {
            "name": name,
            "version": package.version,
            "license": (
                metadata.get("License-Expression")
                or metadata.get("License")
                or "; ".join(classifiers)
                or "UNKNOWN"
            ),
        }
    return [result[name] for name in sorted(result)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _distribution_license_files(name: str) -> dict[str, Path]:
    package = distribution(name)
    result: dict[str, Path] = {}
    for item in package.files or ():
        parts = PurePosixPath(str(item)).parts
        try:
            dist_info = next(
                index for index, part in enumerate(parts) if part.endswith(".dist-info")
            )
        except StopIteration:
            continue
        if len(parts) <= dist_info + 2 or parts[dist_info + 1] != "licenses":
            continue
        relative = PurePosixPath(*parts[dist_info + 2 :]).as_posix()
        if not relative or relative.startswith("../"):
            continue
        source = Path(package.locate_file(item))
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"license payload is not a regular file: {name}:{relative}")
        result[relative] = source
    return result


def _locked_hashes(lock_path: Path, name: str, version: str) -> set[str]:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    for package in lock.get("package", []):
        if normalized(package.get("name", "")) == normalized(name) and package.get("version") == version:
            artifacts = [package.get("sdist", {}), *package.get("wheels", [])]
            return {item["hash"] for item in artifacts if item.get("hash")}
    return set()


def _validate_rule(
    *,
    name: str,
    rule: dict[str, Any],
    package: dict[str, str] | None,
    lock_path: Path,
    errors: list[str],
) -> None:
    if package is None:
        errors.append(f"policy dependency is absent: {name}")
        return
    if package["version"] != rule["locked_version"]:
        errors.append(f"{name} version changed: {package['version']} != {rule['locked_version']}")
    for fragment in rule["expected_license_contains"]:
        if fragment not in package["license"]:
            errors.append(f"{name} license metadata no longer contains: {fragment}")
    expected_hashes = set(rule.get("expected_lock_hashes", []))
    if expected_hashes:
        actual_hashes = _locked_hashes(lock_path, name, rule["locked_version"])
        for expected in sorted(expected_hashes - actual_hashes):
            errors.append(f"{name} locked artifact hash is absent: {expected}")


def _validate_notice_payload(
    *, name: str, rule: dict[str, Any], errors: list[str]
) -> tuple[dict[str, Any], dict[str, Path]] | None:
    review = rule.get("release_review")
    payload = review.get("required_license_payload") if isinstance(review, dict) else None
    expected = payload.get("files_sha256") if isinstance(payload, dict) else None
    if not isinstance(expected, dict) or not expected:
        errors.append(f"{name} exact license payload file map is missing or invalid")
        return None
    invalid_entry = False
    for relative, digest in expected.items():
        if not isinstance(relative, str):
            errors.append(f"{name} exact license payload path is not a string")
            invalid_entry = True
            continue
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not relative:
            errors.append(f"{name} exact license payload path is invalid: {relative}")
            invalid_entry = True
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append(f"{name} exact license payload hash is invalid: {relative}")
            invalid_entry = True
    if invalid_entry:
        return None
    try:
        actual = _distribution_license_files(name)
    except (PackageNotFoundError, RuntimeError) as error:
        errors.append(str(error))
        return None
    expected_paths = set(expected)
    actual_paths = set(actual)
    for path in sorted(expected_paths - actual_paths):
        errors.append(f"{name} license payload file is missing: {path}")
    for path in sorted(actual_paths - expected_paths):
        errors.append(f"{name} license payload has an undeclared file: {path}")
    files = []
    for path in sorted(expected_paths & actual_paths):
        digest = _sha256(actual[path])
        if digest != expected[path]:
            errors.append(f"{name} license payload hash changed: {path}")
        files.append({"path": path, "sha256": digest})
    return {"name": normalized(name), "files": files}, actual


def _notice_anchors(body: str) -> set[str]:
    result: set[str] = set()
    for line in body.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        heading = match.group(1).strip().lower()
        heading = re.sub(r"[^\w\- ]", "", heading)
        result.add(re.sub(r"\s+", "-", heading))
    return result


def _validate_release_review_policy(
    policy: dict[str, Any], errors: list[str]
) -> dict[str, Any] | None:
    review_policy = policy.get("release_review_policy")
    expected_fields = {
        "semantics",
        "allowed_decisions",
        "required_payload_source",
        "notice_reference_path",
    }
    if not isinstance(review_policy, dict) or set(review_policy) != expected_fields:
        errors.append("release review policy is missing or does not match the required schema")
        return None
    if review_policy.get("semantics") != _RELEASE_REVIEW_SEMANTICS:
        errors.append("release review policy semantics are missing or overstated")
    if review_policy.get("allowed_decisions") != [_RELEASE_DECISION]:
        errors.append("release review policy decisions do not match the bounded decision set")
    if review_policy.get("required_payload_source") != _RELEASE_PAYLOAD_SOURCE:
        errors.append("release review policy payload source does not match the installed licenses")
    reference_path = review_policy.get("notice_reference_path")
    if reference_path != "THIRD_PARTY_NOTICES.md":
        errors.append("release review policy notice path does not match the shipped notice")
    return review_policy


def _validate_release_review(
    *,
    name: str,
    rule: dict[str, Any],
    review_policy: dict[str, Any] | None,
    notice_body: str | None,
    errors: list[str],
) -> dict[str, Any] | None:
    if review_policy is None:
        errors.append("release review policy is missing or invalid")
        return None
    review = rule.get("release_review")
    if not isinstance(review, dict):
        errors.append(f"{name} release review record is missing or invalid")
        return None
    expected_review_fields = {"decision", "reference", "required_license_payload"}
    if set(review) != expected_review_fields:
        errors.append(f"{name} release review fields do not match policy schema")

    decision = review.get("decision")
    allowed_decisions = review_policy.get("allowed_decisions")
    if (
        decision != _RELEASE_DECISION
        or not isinstance(allowed_decisions, list)
        or decision not in allowed_decisions
    ):
        errors.append(f"{name} release decision is missing or not allowed: {decision}")

    reference = review.get("reference")
    reference_path = review_policy.get("notice_reference_path")
    reference_anchor: str | None = None
    if (
        not isinstance(reference, str)
        or not isinstance(reference_path, str)
        or not reference.startswith(f"{reference_path}#")
    ):
        errors.append(f"{name} release reference does not target the required notice")
    else:
        reference_anchor = reference.partition("#")[2]
        if not reference_anchor or notice_body is None or reference_anchor not in _notice_anchors(notice_body):
            errors.append(f"{name} release reference anchor does not exist: {reference}")

    payload = review.get("required_license_payload")
    expected_payload_fields = {"source", "files_sha256"}
    files: dict[str, str] | None = None
    if not isinstance(payload, dict) or set(payload) != expected_payload_fields:
        errors.append(f"{name} required release license payload is missing or invalid")
    else:
        required_source = review_policy.get("required_payload_source")
        if payload.get("source") != required_source:
            errors.append(f"{name} release license payload source does not match policy")
        candidate_files = payload.get("files_sha256")
        if not isinstance(candidate_files, dict) or not candidate_files:
            errors.append(f"{name} required release license file set is empty or invalid")
        else:
            files = candidate_files
            for relative, digest in files.items():
                path = PurePosixPath(relative)
                if path.is_absolute() or ".." in path.parts or not relative:
                    errors.append(f"{name} required release license path is invalid: {relative}")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    errors.append(f"{name} required release license hash is invalid: {relative}")

    if not isinstance(decision, str) or not isinstance(reference, str) or files is None:
        return None
    return {
        "decision": decision,
        "reference": reference,
        "required_license_files": sorted(files),
    }


def _license_payload(
    package: dict[str, str], errors: list[str]
) -> tuple[dict[str, Any], dict[str, Path]]:
    name = package["name"]
    try:
        sources = _distribution_license_files(name)
    except (PackageNotFoundError, RuntimeError) as error:
        errors.append(str(error))
        sources = {}
    return (
        {
            **package,
            "files": [
                {"path": path, "sha256": _sha256(source)}
                for path, source in sorted(sources.items())
            ],
        },
        sources,
    )


def _validate_project_notice(
    policy: dict[str, Any], notices_path: Path, errors: list[str]
) -> str | None:
    notice_policy = policy["third_party_notices"]
    try:
        body = notices_path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"third-party notice cannot be read: {error}")
        return None
    for fragment in notice_policy["required_fragments"]:
        if fragment not in body:
            errors.append(f"third-party notice is missing required text: {fragment}")
    return body


def _stage_notices(
    *,
    output_root: Path,
    notices_path: Path,
    payload_sources: dict[str, dict[str, Path]],
    manifest: dict[str, Any],
) -> None:
    if output_root.exists() and any(output_root.iterdir()):
        raise RuntimeError(f"notice output directory is not empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(notices_path, output_root / "THIRD_PARTY_NOTICES.md")
    for name, sources in sorted(payload_sources.items()):
        destination_root = output_root / normalized(name)
        for relative, source in sorted(sources.items()):
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    (output_root / "dependency-license-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def check(
    *,
    pyproject: Path,
    policy_path: Path,
    lock_path: Path,
    notices_path: Path,
    output: Path | None,
    release: bool,
    notices_only: bool = False,
    notice_output_root: Path | None = None,
) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    packages = installed_dependencies() if notices_only else runtime_dependencies(pyproject)
    by_name = {item["name"]: item for item in packages}
    errors: list[str] = []
    reviews: list[dict[str, str]] = []
    review_resolutions: list[dict[str, Any]] = []
    review_triggers = tuple(item.upper() for item in policy["review_triggers"])
    if not notices_only:
        required = {
            item["name"]
            for item in packages
            if any(trigger in item["license"].upper() for trigger in review_triggers)
        }
        declared = {normalized(name) for name in policy["review_required"]}
        for name in sorted(required - declared):
            errors.append(f"review-triggering dependency is not declared in policy: {name}")

    notice_body = _validate_project_notice(policy, notices_path, errors)
    release_policy = _validate_release_review_policy(policy, errors) if release else None
    release_globally_blocked = release and bool(errors)

    payloads: list[dict[str, Any]] = []
    payload_sources: dict[str, dict[str, Path]] = {}
    for name, rule in policy["review_required"].items():
        prior_errors = len(errors)
        package = by_name.get(normalized(name))
        _validate_rule(name=name, rule=rule, package=package, lock_path=lock_path, errors=errors)
        _validate_notice_payload(name=name, rule=rule, errors=errors)
        release_record = None
        if release:
            release_record = _validate_release_review(
                name=name,
                rule=rule,
                review_policy=release_policy,
                notice_body=notice_body,
                errors=errors,
            )
        if package is not None:
            if (
                release
                and not release_globally_blocked
                and len(errors) == prior_errors
                and release_record is not None
            ):
                review_resolutions.append({**package, **release_record})
            else:
                reviews.append(package)

    for package in packages:
        details, sources = _license_payload(package, errors)
        payloads.append(details)
        if sources:
            payload_sources[package["name"]] = sources

    report = {
        "schema": "cardrag.dependency-license-inventory.v3",
        "project_license": policy["project_license"],
        "mode": (
            "release-notices-only"
            if notices_only and release
            else "notices-only"
            if notices_only
            else "release"
            if release
            else "inventory"
        ),
        "packages": packages,
        "manual_review_required": reviews,
        "manual_review_resolved": review_resolutions,
        "license_payloads": payloads,
        "status": "failed" if errors else "passed",
        "errors": errors,
    }
    if notice_output_root is not None and not errors:
        try:
            _stage_notices(
                output_root=notice_output_root,
                notices_path=notices_path,
                payload_sources=payload_sources,
                manifest=report,
            )
        except (OSError, RuntimeError) as error:
            errors.append(f"license notice staging failed: {error}")
            report["status"] = "failed"
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
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--notices", type=Path, default=Path("THIRD_PARTY_NOTICES.md"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--notices-only", action="store_true")
    parser.add_argument("--notice-output-root", type=Path)
    args = parser.parse_args()
    report = check(
        pyproject=args.pyproject,
        policy_path=args.policy,
        lock_path=args.lock,
        notices_path=args.notices,
        output=args.output,
        release=args.release,
        notices_only=args.notices_only,
        notice_output_root=args.notice_output_root,
    )
    if report["status"] != "passed":
        for error in report["errors"]:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
