#!/usr/bin/env python3
"""Fail closed around the bounded pgvector/PostgreSQL image exception."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "security" / "pgvector-image-vex.json"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CVE_RE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}")
_ALLOWED_SEVERITIES = {"HIGH", "CRITICAL"}


class ValidationError(ValueError):
    """Raised when a policy or scanner report does not match the closed schema."""


@dataclass(frozen=True)
class Finding:
    severity: str
    scanner_status: str


@dataclass(frozen=True)
class Policy:
    image_reference: str
    canonical_digest_reference: str
    image_digest: str
    linux_amd64_config_digest: str
    artifact_type: str
    binary_target: str
    binary_sha256: str
    component: str
    installed_version: str
    result_class: str
    result_type: str
    report_schema_version: int
    expires_on: date
    impact_statement: str
    findings: dict[str, Finding]


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be an array")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{path} must be a non-empty string")
    return value


def _integer(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ValidationError(f"{path} must be an integer")
    return value


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{path} must be a boolean")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValidationError(f"{path} fields do not match schema: missing={missing}, extra={extra}")


def _parse_date(value: Any, path: str) -> date:
    text = _string(value, path)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise ValidationError(f"{path} must be an ISO-8601 calendar date") from error
    if parsed.isoformat() != text:
        raise ValidationError(f"{path} must use YYYY-MM-DD format")
    return parsed


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValidationError(f"cannot read {label}: {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValidationError(f"{label} is not valid JSON: {path}: {error}") from error
    return _object(raw, label)


def _parse_policy(raw: dict[str, Any]) -> Policy:
    _exact_keys(raw, {"schema_version", "kind", "scope", "assessment", "findings"}, "policy")
    if _integer(raw["schema_version"], "policy.schema_version") != 1:
        raise ValidationError("policy.schema_version must be 1")
    if _string(raw["kind"], "policy.kind") != "cardrag-bounded-vulnerability-exception":
        raise ValidationError("policy.kind is unsupported")

    scope = _object(raw["scope"], "policy.scope")
    _exact_keys(scope, {"image", "binary", "scanner"}, "policy.scope")
    image = _object(scope["image"], "policy.scope.image")
    _exact_keys(
        image,
        {
            "reference",
            "canonical_digest_reference",
            "digest",
            "linux_amd64_config_digest",
            "artifact_type",
        },
        "policy.scope.image",
    )
    image_reference = _string(image["reference"], "policy.scope.image.reference")
    canonical_reference = _string(
        image["canonical_digest_reference"],
        "policy.scope.image.canonical_digest_reference",
    )
    image_digest = _string(image["digest"], "policy.scope.image.digest")
    if _DIGEST_RE.fullmatch(image_digest) is None:
        raise ValidationError("policy.scope.image.digest must be a lowercase sha256 digest")
    if not image_reference.endswith(f"@{image_digest}"):
        raise ValidationError("policy image reference is not bound to its digest")
    if not canonical_reference.endswith(f"@{image_digest}"):
        raise ValidationError("policy canonical image reference is not bound to its digest")
    linux_amd64_config_digest = _string(
        image["linux_amd64_config_digest"],
        "policy.scope.image.linux_amd64_config_digest",
    )
    if _DIGEST_RE.fullmatch(linux_amd64_config_digest) is None:
        raise ValidationError(
            "policy.scope.image.linux_amd64_config_digest must be a lowercase sha256 digest"
        )
    if linux_amd64_config_digest == image_digest:
        raise ValidationError("policy image index and linux/amd64 config digests must be distinct")
    artifact_type = _string(image["artifact_type"], "policy.scope.image.artifact_type")
    if artifact_type != "container_image":
        raise ValidationError("policy.scope.image.artifact_type must be container_image")

    binary = _object(scope["binary"], "policy.scope.binary")
    _exact_keys(
        binary,
        {
            "target",
            "container_path",
            "sha256",
            "component",
            "installed_version",
            "gosu_version",
            "platform",
            "result_class",
            "result_type",
        },
        "policy.scope.binary",
    )
    binary_target = _string(binary["target"], "policy.scope.binary.target")
    if _string(binary["container_path"], "policy.scope.binary.container_path") != f"/{binary_target}":
        raise ValidationError("policy binary target and container path disagree")
    binary_sha256 = _string(binary["sha256"], "policy.scope.binary.sha256")
    if _SHA256_RE.fullmatch(binary_sha256) is None:
        raise ValidationError("policy.scope.binary.sha256 must be a lowercase sha256 hash")
    component = _string(binary["component"], "policy.scope.binary.component")
    installed_version = _string(binary["installed_version"], "policy.scope.binary.installed_version")
    if _string(binary["gosu_version"], "policy.scope.binary.gosu_version") != "1.19":
        raise ValidationError("policy.scope.binary.gosu_version must be 1.19")
    if _string(binary["platform"], "policy.scope.binary.platform") != "linux/amd64":
        raise ValidationError("policy.scope.binary.platform must be linux/amd64")
    result_class = _string(binary["result_class"], "policy.scope.binary.result_class")
    result_type = _string(binary["result_type"], "policy.scope.binary.result_type")

    scanner = _object(scope["scanner"], "policy.scope.scanner")
    _exact_keys(
        scanner,
        {"name", "report_schema_version", "ignore_unfixed", "severity_filter"},
        "policy.scope.scanner",
    )
    if _string(scanner["name"], "policy.scope.scanner.name") != "Trivy":
        raise ValidationError("policy.scope.scanner.name must be Trivy")
    report_schema_version = _integer(
        scanner["report_schema_version"], "policy.scope.scanner.report_schema_version"
    )
    if not _boolean(scanner["ignore_unfixed"], "policy.scope.scanner.ignore_unfixed"):
        raise ValidationError("policy scanner ignore_unfixed must be true")
    severity_filter = _array(scanner["severity_filter"], "policy.scope.scanner.severity_filter")
    if severity_filter != ["HIGH", "CRITICAL"]:
        raise ValidationError("policy scanner severity filter must be exactly HIGH,CRITICAL")

    assessment = _object(raw["assessment"], "policy.assessment")
    _exact_keys(
        assessment,
        {
            "vex_status",
            "justification",
            "method",
            "reviewed_on",
            "expires_on",
            "impact_statement",
            "reachability_rationale",
            "required_action",
        },
        "policy.assessment",
    )
    if _string(assessment["vex_status"], "policy.assessment.vex_status") != "not_affected":
        raise ValidationError("policy assessment status must be not_affected")
    if (
        _string(assessment["justification"], "policy.assessment.justification")
        != "vulnerable_code_not_in_execute_path"
    ):
        raise ValidationError("policy assessment justification is unsupported")
    if _string(assessment["method"], "policy.assessment.method") != "manual_static_reachability_review":
        raise ValidationError("policy assessment method is unsupported")
    reviewed_on = _parse_date(assessment["reviewed_on"], "policy.assessment.reviewed_on")
    expires_on = _parse_date(assessment["expires_on"], "policy.assessment.expires_on")
    if reviewed_on > expires_on:
        raise ValidationError("policy review date is after its expiry date")
    impact_statement = _string(assessment["impact_statement"], "policy.assessment.impact_statement")
    required_impact_phrases = (
        "--ignore-unfixed",
        "does not evaluate affected, fix_deferred, or will_not_fix findings",
        "does not assert that the image has zero vulnerabilities",
    )
    if any(phrase not in impact_statement for phrase in required_impact_phrases):
        raise ValidationError(
            "policy impact statement must bound ignore-unfixed scope and not claim zero vulnerabilities"
        )
    rationale = _array(assessment["reachability_rationale"], "policy.assessment.reachability_rationale")
    if len(rationale) < 3 or any(not isinstance(item, str) or not item for item in rationale):
        raise ValidationError("policy reachability rationale must contain at least three statements")
    required_action = _string(assessment["required_action"], "policy.assessment.required_action")
    if (
        "fixed-finding release gate only" not in required_action
        or "non-fixed findings" not in required_action
    ):
        raise ValidationError("policy required action must preserve separate non-fixed finding review")

    finding_items = _array(raw["findings"], "policy.findings")
    if not finding_items:
        raise ValidationError("policy.findings must not be empty")
    findings: dict[str, Finding] = {}
    for index, item in enumerate(finding_items):
        path = f"policy.findings[{index}]"
        finding = _object(item, path)
        _exact_keys(finding, {"id", "severity", "scanner_status"}, path)
        finding_id = _string(finding["id"], f"{path}.id")
        if _CVE_RE.fullmatch(finding_id) is None:
            raise ValidationError(f"{path}.id must be a CVE identifier")
        if finding_id in findings:
            raise ValidationError(f"policy has duplicate finding: {finding_id}")
        severity = _string(finding["severity"], f"{path}.severity")
        if severity not in _ALLOWED_SEVERITIES:
            raise ValidationError(f"policy finding has unsupported severity: {finding_id}:{severity}")
        scanner_status = _string(finding["scanner_status"], f"{path}.scanner_status")
        if scanner_status != "fixed":
            raise ValidationError(f"policy finding scanner status must be fixed: {finding_id}")
        findings[finding_id] = Finding(severity=severity, scanner_status=scanner_status)

    return Policy(
        image_reference=image_reference,
        canonical_digest_reference=canonical_reference,
        image_digest=image_digest,
        linux_amd64_config_digest=linux_amd64_config_digest,
        artifact_type=artifact_type,
        binary_target=binary_target,
        binary_sha256=binary_sha256,
        component=component,
        installed_version=installed_version,
        result_class=result_class,
        result_type=result_type,
        report_schema_version=report_schema_version,
        expires_on=expires_on,
        impact_statement=impact_statement,
        findings=findings,
    )


def _validate_report(raw: dict[str, Any], policy: Policy) -> None:
    schema_version = _integer(raw.get("SchemaVersion"), "report.SchemaVersion")
    if schema_version != policy.report_schema_version:
        raise ValidationError(
            f"report schema version changed: {schema_version} != {policy.report_schema_version}"
        )
    artifact_name = _string(raw.get("ArtifactName"), "report.ArtifactName")
    if artifact_name != policy.image_reference:
        raise ValidationError(f"report image reference changed: {artifact_name} != {policy.image_reference}")
    artifact_type = _string(raw.get("ArtifactType"), "report.ArtifactType")
    if artifact_type != policy.artifact_type:
        raise ValidationError(f"report artifact type changed: {artifact_type} != {policy.artifact_type}")

    metadata = _object(raw.get("Metadata"), "report.Metadata")
    image_id = _string(metadata.get("ImageID"), "report.Metadata.ImageID")
    # With Docker's classic image store Trivy reports the linux/amd64 OCI
    # config digest here; with the containerd image store it reports the OCI
    # index digest. Both immutable digests are bound explicitly by the policy.
    accepted_image_ids = {policy.image_digest, policy.linux_amd64_config_digest}
    if image_id not in accepted_image_ids:
        raise ValidationError(
            f"report image digest changed: {image_id} not in {sorted(accepted_image_ids)}"
        )
    # Trivy 0.67.2 omits Metadata.Reference for some Docker-socket scans. When
    # emitted it remains useful corroborating evidence and must match exactly;
    # identity is always bound below through ArtifactName, ImageID, and
    # RepoDigests.
    if "Reference" in metadata:
        reference = _string(metadata["Reference"], "report.Metadata.Reference")
        if reference != policy.canonical_digest_reference:
            raise ValidationError(
                "report canonical digest reference changed: "
                f"{reference} != {policy.canonical_digest_reference}"
            )
    repo_digests = _array(metadata.get("RepoDigests"), "report.Metadata.RepoDigests")
    if not repo_digests or any(not isinstance(item, str) for item in repo_digests):
        raise ValidationError("report.Metadata.RepoDigests must contain string references")
    if policy.canonical_digest_reference not in repo_digests:
        raise ValidationError("report repository digests do not contain the policy-bound digest")
    if any(not item.endswith(f"@{policy.image_digest}") for item in repo_digests):
        raise ValidationError("report repository digests contain a different image digest")

    results = _array(raw.get("Results"), "report.Results")
    if not results:
        raise ValidationError("report.Results must not be empty")
    actual: dict[str, Finding] = {}
    for result_index, result_value in enumerate(results):
        result_path = f"report.Results[{result_index}]"
        result = _object(result_value, result_path)
        target = _string(result.get("Target"), f"{result_path}.Target")
        result_class = _string(result.get("Class"), f"{result_path}.Class")
        result_type = _string(result.get("Type"), f"{result_path}.Type")
        # Trivy omits this field entirely for result sections with no findings.
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            continue
        finding_values = _array(vulnerabilities, f"{result_path}.Vulnerabilities")
        for finding_index, finding_value in enumerate(finding_values):
            finding_path = f"{result_path}.Vulnerabilities[{finding_index}]"
            finding = _object(finding_value, finding_path)
            finding_id = _string(finding.get("VulnerabilityID"), f"{finding_path}.VulnerabilityID")
            if _CVE_RE.fullmatch(finding_id) is None:
                raise ValidationError(f"{finding_path}.VulnerabilityID must be a CVE identifier")
            if finding_id in actual:
                raise ValidationError(f"report has duplicate finding: {finding_id}")
            if (
                target != policy.binary_target
                or result_class != policy.result_class
                or result_type != policy.result_type
            ):
                raise ValidationError(
                    "report finding is outside the exception target: "
                    f"{finding_id}:{target}:{result_class}:{result_type}"
                )
            package = _string(finding.get("PkgName"), f"{finding_path}.PkgName")
            if package != policy.component:
                raise ValidationError(
                    f"report finding package changed: {finding_id}:{package} != {policy.component}"
                )
            installed_version = _string(finding.get("InstalledVersion"), f"{finding_path}.InstalledVersion")
            if installed_version != policy.installed_version:
                raise ValidationError(
                    "report finding version changed: "
                    f"{finding_id}:{installed_version} != {policy.installed_version}"
                )
            severity = _string(finding.get("Severity"), f"{finding_path}.Severity")
            scanner_status = _string(finding.get("Status"), f"{finding_path}.Status")
            actual[finding_id] = Finding(severity=severity, scanner_status=scanner_status)

    expected_ids = set(policy.findings)
    actual_ids = set(actual)
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing or extra:
        raise ValidationError(f"report finding set changed: missing={missing}, extra={extra}")
    for finding_id, expected in policy.findings.items():
        observed = actual[finding_id]
        if observed.severity != expected.severity:
            raise ValidationError(
                f"report finding severity changed: {finding_id}:{observed.severity} != {expected.severity}"
            )
        if observed.scanner_status != expected.scanner_status:
            raise ValidationError(
                "report finding status changed: "
                f"{finding_id}:{observed.scanner_status} != {expected.scanner_status}"
            )


def check(
    *,
    policy_path: Path,
    report_path: Path,
    binary_sha256: str,
    today: date | None = None,
) -> dict[str, Any]:
    """Validate one scanner report and binary hash against the bounded policy."""

    checked_on = today or datetime.now(tz=UTC).date()
    try:
        policy = _parse_policy(_load_json(policy_path, "policy"))
        if checked_on > policy.expires_on:
            raise ValidationError(
                f"policy expired on {policy.expires_on.isoformat()} (checked {checked_on.isoformat()})"
            )
        if _SHA256_RE.fullmatch(binary_sha256) is None:
            raise ValidationError("observed binary sha256 must be 64 lowercase hexadecimal characters")
        if binary_sha256 != policy.binary_sha256:
            raise ValidationError(f"binary sha256 changed: {binary_sha256} != {policy.binary_sha256}")
        _validate_report(_load_json(report_path, "report"), policy)
    except ValidationError as error:
        return {
            "status": "failed",
            "checked_on": checked_on.isoformat(),
            "policy_path": str(policy_path),
            "report_path": str(report_path),
            "errors": [str(error)],
        }
    return {
        "status": "passed-with-bounded-exception",
        "checked_on": checked_on.isoformat(),
        "policy_path": str(policy_path),
        "report_path": str(report_path),
        "image_digest": policy.image_digest,
        "binary_target": policy.binary_target,
        "binary_sha256": binary_sha256,
        "finding_count": len(policy.findings),
        "exception_expires_on": policy.expires_on.isoformat(),
        "impact_statement": policy.impact_statement,
        "errors": [],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the bounded pgvector/PostgreSQL image vulnerability exception."
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = check(
        policy_path=args.policy,
        report_path=args.report,
        binary_sha256=args.binary_sha256,
    )
    body = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    sys.stdout.write(body)
    return 0 if result["status"] == "passed-with-bounded-exception" else 1


if __name__ == "__main__":
    raise SystemExit(main())
