"""Lightweight, fail-closed release-qualification evidence for the public 1.0.32 release.

Per handoff/005 FIX_03 the publish gate binds only verifiable public artifacts:
the final source commit, the two candidate OCI index digests, the passing CI
run, and an explicit list of verifications that were *not* performed. It never
claims a candidate worker run, twelve-tool MCP execution, gold quality
evaluation, or a production cutover. The operational 03:00 run is recorded as
a reference from a different source commit and is never re-presented as
candidate runtime proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Final, Literal, Self, cast

from pydantic import StringConstraints, ValidationError, field_validator, model_validator

from .candidate_acceptance import ImageDigest, Sha256Hex, SourceCommit, _CanonicalModel
from .canonical import canonical_json_bytes
from .paths import validate_identifier

QUALIFICATION_SCHEMA: Final = "cardrag.release-qualification.v1"
VALIDATION_SCHEMA: Final = "cardrag.release-qualification-validation.v1"
_MAX_QUALIFICATION_BYTES: Final = 2 * 1024 * 1024

NOT_PERFORMED: Final = (
    "candidate_mcp_12_tools",
    "candidate_worker_full_run",
    "gold_quality_evaluation",
    "production_cutover",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID32 = re.compile(r"^[0-9a-f]{32}$")


class ReleaseQualificationError(RuntimeError):
    """A bounded validation failure which never includes evidence contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReleaseQualificationCI(_CanonicalModel):
    commit: SourceCommit
    conclusion: Literal["success"]
    run_url: Annotated[
        str,
        StringConstraints(
            pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[1-9][0-9]*$"
        ),
    ]


class OperationalRunReference(_CanonicalModel):
    generation_id: str
    image_source_commit: SourceCommit
    interpretation: Literal["reference_only_not_candidate_runtime"]
    worker_run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]

    @field_validator("generation_id")
    @classmethod
    def generation_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, label="generation_id")


class ReleaseQualification(_CanonicalModel):
    schema_version: Literal["cardrag.release-qualification.v1"]
    release_version: Literal["1.0.32"]
    source_commit: SourceCommit
    worker_image_digest: ImageDigest
    mcp_image_digest: ImageDigest
    ci: ReleaseQualificationCI
    operational_reference: OperationalRunReference
    not_performed: tuple[str, ...]

    @model_validator(mode="after")
    def exact_operational_contract(self) -> Self:
        if self.not_performed != NOT_PERFORMED:
            raise ValueError("release qualification must record the exact four not-performed checks")
        if self.worker_image_digest == self.mcp_image_digest:
            raise ValueError("Worker and MCP candidate digests must differ")
        if self.ci.commit != self.source_commit:
            raise ValueError("CI run must belong to the qualified source commit")
        if self.operational_reference.image_source_commit == self.source_commit:
            raise ValueError("operational reference must come from a different source commit")
        return self


class ReleaseQualificationValidation(_CanonicalModel):
    schema_version: Literal["cardrag.release-qualification-validation.v1"]
    status: Literal["qualified"]
    qualification_sha256: Sha256Hex
    source_commit: SourceCommit
    worker_image_digest: ImageDigest
    mcp_image_digest: ImageDigest


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def verify_release_qualification(
    qualification_path: Path,
    *,
    expected_sha256: str,
    expected_source_commit: str,
    expected_worker_image_digest: str,
    expected_mcp_image_digest: str,
    expected_repository: str,
) -> ReleaseQualificationValidation:
    """Read one regular file, re-verify every binding, and never echo contents."""

    if len(expected_sha256) != 64 or not _HEX64.fullmatch(expected_sha256):
        raise ReleaseQualificationError("expected_sha256_invalid")
    if len(expected_source_commit) != 40 or not re.fullmatch(r"[0-9a-f]{40}", expected_source_commit):
        raise ReleaseQualificationError("expected_source_commit_invalid")
    for digest in (expected_worker_image_digest, expected_mcp_image_digest):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ReleaseQualificationError("expected_image_digest_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected_repository):
        raise ReleaseQualificationError("expected_repository_invalid")

    try:
        listed = qualification_path.lstat()
    except OSError:
        raise ReleaseQualificationError("qualification_unreadable") from None
    if not stat.S_ISREG(listed.st_mode) or listed.st_size == 0 or listed.st_size > _MAX_QUALIFICATION_BYTES:
        raise ReleaseQualificationError("qualification_file_invalid")
    raw = qualification_path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    if sha256 != expected_sha256:
        raise ReleaseQualificationError("qualification_sha256_mismatch")
    try:
        json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise ReleaseQualificationError("qualification_json_invalid") from None
    try:
        qualification = ReleaseQualification.model_validate_json(raw)
    except ValidationError:
        raise ReleaseQualificationError("qualification_schema_invalid") from None
    canonical = canonical_json_bytes(qualification) + b"\n"
    if canonical != raw:
        raise ReleaseQualificationError("qualification_not_canonical")
    if qualification.source_commit != expected_source_commit:
        raise ReleaseQualificationError("qualification_source_commit_mismatch")
    if qualification.worker_image_digest != expected_worker_image_digest:
        raise ReleaseQualificationError("qualification_worker_digest_mismatch")
    if qualification.mcp_image_digest != expected_mcp_image_digest:
        raise ReleaseQualificationError("qualification_mcp_digest_mismatch")
    expected_prefix = f"https://github.com/{expected_repository}/actions/runs/"
    if not qualification.ci.run_url.startswith(expected_prefix):
        raise ReleaseQualificationError("qualification_ci_run_foreign_repository")
    if not qualification.ci.conclusion == "success":  # literal already guarantees, kept explicit
        raise ReleaseQualificationError("qualification_ci_not_success")
    return ReleaseQualificationValidation(
        schema_version=VALIDATION_SCHEMA,
        status="qualified",
        qualification_sha256=sha256,
        source_commit=qualification.source_commit,
        worker_image_digest=qualification.worker_image_digest,
        mcp_image_digest=qualification.mcp_image_digest,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-worker-image-digest", required=True)
    parser.add_argument("--expected-mcp-image-digest", required=True)
    parser.add_argument("--expected-repository", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        validation = verify_release_qualification(
            cast(Path, arguments.qualification),
            expected_sha256=cast(str, arguments.expected_sha256),
            expected_source_commit=cast(str, arguments.expected_source_commit),
            expected_worker_image_digest=cast(str, arguments.expected_worker_image_digest),
            expected_mcp_image_digest=cast(str, arguments.expected_mcp_image_digest),
            expected_repository=cast(str, arguments.expected_repository),
        )
    except ReleaseQualificationError:
        print("release qualification validation failed", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(validation.canonical_bytes())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

__all__ = [
    "NOT_PERFORMED",
    "QUALIFICATION_SCHEMA",
    "VALIDATION_SCHEMA",
    "OperationalRunReference",
    "ReleaseQualification",
    "ReleaseQualificationCI",
    "ReleaseQualificationError",
    "ReleaseQualificationValidation",
    "main",
    "verify_release_qualification",
]
