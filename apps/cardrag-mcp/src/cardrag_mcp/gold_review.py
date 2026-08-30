"""Offline human-review tooling for CardRAG gold labels and blind A/B ratings.

The draft sampler reads only the sealed v5 corpus inventory.  It deliberately
does not import serving/search implementations, score artifacts, or provider
clients, so candidate performance cannot influence gold selection.  Human
approval is mandatory before a release gold file can be sealed.

The review server is a loopback-only convenience UI.  It serves no external
assets, does not expose input paths, and keeps blind lane identity and candidate
position outside the review packet and resumable review state.
"""

# The embedded self-contained HTML/JavaScript intentionally has a few long physical lines.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast
from urllib.parse import urlsplit

from cardrag_core import canonical_json_bytes, canonical_sha256
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from cardrag_mcp.evaluation import (
    GOLD_SCHEMA_VERSION,
    MAX_BLIND_RATINGS_PER_QUERY,
    MAX_JSONL_LINE_BYTES,
    MAX_RELEASE_QUERIES,
    MIN_RELEASE_QUERIES,
    REQUIRED_RELEASE_SLICES,
    BlindEvaluationManifest,
    BlindPairwiseRating,
    EvaluationError,
    EvidenceRole,
    GoldContract,
    GoldQuery,
    GoldSpan,
    PairwisePreference,
    RunDataset,
    load_blind_evaluation_jsonl,
    load_gold_jsonl,
    load_run_jsonl,
)
from cardrag_mcp.schema_v5 import ServingDatabaseV5Error, validate_schema_v5

ISSUERS: tuple[str, ...] = ("kb", "samsung", "shinhan", "woori")
DEFAULT_QUERY_COUNT = 300
DEFAULT_NO_ANSWER_COUNT = 24
DEFAULT_SEED = 1010
MAX_REVIEW_BODY_BYTES = 1024 * 1024
MAX_DATABASE_BYTES = 64 * 1024 * 1024 * 1024
MAX_REVIEW_FILE_BYTES = 256 * 1024 * 1024

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SliceName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._:-]{0,63}$"),
]
ReviewStatus = Literal["pending", "approved", "rejected"]

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NUMERIC_FACT = re.compile(
    r"(?<![0-9A-Za-z가-힣])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(?:만원|천원|억원|원|%|퍼센트|개월|일|회|건|점|마일|시간)(?![0-9A-Za-z가-힣])"
)

_DOMAIN_SLICES: tuple[str, ...] = (
    "benefit",
    "earning",
    "discount",
    "cashback",
    "performance",
    "exclusion",
    "limit",
    "frequency",
    "minimum_payment",
    "annual_fee",
    "issuance_condition",
    "foreign_fee",
    "negation",
    "exception",
    "grace_period",
    "table",
    "footnote",
    "cross_page",
    "common_notice",
    "hard_negative",
    "current_history",
    "product_specific",
    "discovery_recommendation",
    "comparison",
    "long",
)

_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "benefit": ("혜택", "서비스", "제공"),
    "earning": ("적립", "포인트", "마일"),
    "discount": ("할인", "청구할인"),
    "cashback": ("캐시백", "환급"),
    "performance": ("실적", "이용금액", "전월"),
    "exclusion": ("제외", "미포함", "적용되지", "제공되지"),
    "limit": ("한도", "최대", "월간"),
    "frequency": ("횟수", "회", "일 1", "월 1"),
    "minimum_payment": ("최소", "이상", "건당"),
    "annual_fee": ("연회비", "기본연회비"),
    "issuance_condition": ("발급", "신청", "자격"),
    "foreign_fee": ("해외", "수수료", "국제브랜드"),
    "negation": ("않", "없", "불가", "제외"),
    "exception": ("단,", "예외", "다만"),
    "grace_period": ("유예", "기간", "개월"),
    "common_notice": ("유의사항", "안내", "공통", "주의"),
    "hard_negative": ("제외", "예외", "유의"),
    "current_history": ("변경", "개정", "시행", "적용"),
}


class GoldReviewError(RuntimeError):
    """A bounded, machine-readable offline review failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class DraftManifest(_StrictModel):
    schema_version: Literal["cardrag.gold-draft-artifact.v1"]
    algorithm: Literal["corpus-inventory-stratified.v1"]
    generation_id: Identifier
    corpus_sha256: Sha256Hex
    serving_database_sha256: Sha256Hex
    serving_database_size_bytes: int = Field(gt=0, le=MAX_DATABASE_BYTES)
    inventory_sha256: Sha256Hex
    seed: int = Field(ge=0, le=2**63 - 1)
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    no_answer_count: int = Field(ge=0, le=MAX_RELEASE_QUERIES)
    issuer_counts: dict[str, int]
    required_slices: tuple[SliceName, ...]
    candidate_performance_selection: Literal[False]
    provider_calls: Literal[False]

    @model_validator(mode="after")
    def counts_and_contract_are_exact(self) -> Self:
        if tuple(sorted(self.issuer_counts)) != ISSUERS:
            raise ValueError("draft must contain exactly the four release issuers")
        if sum(self.issuer_counts.values()) != self.query_count:
            raise ValueError("draft issuer counts must sum to query count")
        if self.no_answer_count > self.query_count:
            raise ValueError("draft no-answer count exceeds query count")
        if self.required_slices != tuple(sorted(REQUIRED_RELEASE_SLICES)):
            raise ValueError("draft required slices differ from the release contract")
        return self


class DraftEvidence(_StrictModel):
    span_id: Identifier
    contract_revision_id: Identifier
    issuer: Literal["kb", "samsung", "shinhan", "woori"]
    product_name: str = Field(min_length=1, max_length=1024)
    product_lineage_id: Identifier
    temporal_status: Literal["current", "superseded", "ambiguous"]
    effective_date: str = Field(min_length=1, max_length=64)
    node_type: Literal[
        "MAJOR_SECTION",
        "ITEM",
        "PARAGRAPH",
        "LIST_ITEM",
        "TABLE",
        "TABLE_ROW",
        "FOOTNOTE",
        "BOILERPLATE",
        "UNCLASSIFIED",
    ]
    major_class: Literal["BENEFIT", "NOTICE", "MIXED", "UNKNOWN"]
    heading: str = Field(max_length=4096)
    page: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    text_sha256: Sha256Hex
    text: str = Field(min_length=1, max_length=65_536)

    @model_validator(mode="after")
    def exact_text_hash_is_bound(self) -> Self:
        if self.source_end <= self.source_start:
            raise ValueError("draft source span must be non-empty")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("draft source text hash mismatch")
        if not self.text.strip() or "\x00" in self.text:
            raise ValueError("draft source text must contain content and be NUL-free")
        return self


class GoldDraftRecord(_StrictModel):
    schema_version: Literal["cardrag.gold-draft-query.v1"]
    ordinal: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    query_id: Identifier
    issuer: Literal["kb", "samsung", "shinhan", "woori"]
    primary_slice: SliceName
    selection_basis: Literal["corpus_inventory_only"]
    proposed_gold: GoldQuery
    evidence: tuple[DraftEvidence, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def proposed_label_is_source_bound(self) -> Self:
        if self.proposed_gold.query_id != self.query_id:
            raise ValueError("draft query ID differs from proposed gold")
        if f"issuer:{self.issuer}" not in self.proposed_gold.slices:
            raise ValueError("draft issuer slice is missing")
        if self.proposed_gold.no_answer:
            if self.evidence:
                raise ValueError("no-answer draft cannot carry positive evidence")
            return self
        if not self.evidence:
            raise ValueError("answerable draft requires exact source evidence")
        _validate_annotation_sources(self.proposed_gold, self)
        return self


class GoldReviewDecision(_StrictModel):
    query_id: Identifier
    status: ReviewStatus
    annotation: GoldQuery

    @model_validator(mode="after")
    def query_identity_is_fixed(self) -> Self:
        if self.annotation.query_id != self.query_id:
            raise ValueError("review annotation changed query ID")
        return self


class GoldReviewState(_StrictModel):
    schema_version: Literal["cardrag.gold-review-state.v1"]
    draft_sha256: Sha256Hex
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    decisions: tuple[GoldReviewDecision, ...]

    @model_validator(mode="after")
    def state_has_exact_query_coverage(self) -> Self:
        if len(self.decisions) != self.query_count:
            raise ValueError("gold review state count mismatch")
        ids = [item.query_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("gold review state has duplicate queries")
        return self


class BlindPacketManifest(_StrictModel):
    schema_version: Literal["cardrag.blind-review-packet.v1"]
    presentation_protocol: Literal["anonymous-a-b.v1"]
    rubric_id: Literal["cardrag.blind-rubric.naturalness-factual-completeness.v1"]
    gold_sha256: Sha256Hex
    source_run_sha256s: tuple[Sha256Hex, Sha256Hex]
    assignment_seed: int = Field(ge=0, le=2**63 - 1)
    assignment_algorithm: Literal["sha256-balanced-per-rater.v1"]
    query_count: int = Field(ge=1, le=MAX_RELEASE_QUERIES)
    ratings_per_query: int = Field(ge=1, le=MAX_BLIND_RATINGS_PER_QUERY)
    pair_count: int = Field(ge=1)
    rater_keys: tuple[Identifier, ...]
    lane_identity_exposed_to_raters: Literal[False]

    @model_validator(mode="after")
    def packet_is_balanced_and_bounded(self) -> Self:
        if self.query_count % 2:
            raise ValueError("exact left/right balance requires an even query count")
        if self.pair_count != self.query_count * self.ratings_per_query:
            raise ValueError("blind review packet count mismatch")
        if len(self.rater_keys) != self.ratings_per_query:
            raise ValueError("one distinct rater key is required per rating round")
        if len(set(self.rater_keys)) != len(self.rater_keys):
            raise ValueError("blind review rater keys must be unique")
        if tuple(sorted(self.source_run_sha256s)) != self.source_run_sha256s:
            raise ValueError("blind review source hashes must be order-independent")
        return self


class BlindPacketPair(_StrictModel):
    schema_version: Literal["cardrag.blind-review-pair.v1"]
    pair_id: Identifier
    query_id: Identifier
    rater_key: Identifier
    question: str = Field(min_length=1, max_length=4096)
    left_answer: str = Field(min_length=1, max_length=65_536)
    left_answer_sha256: Sha256Hex
    right_answer: str = Field(min_length=1, max_length=65_536)
    right_answer_sha256: Sha256Hex

    @model_validator(mode="after")
    def answer_hashes_are_exact(self) -> Self:
        if hashlib.sha256(self.left_answer.encode("utf-8")).hexdigest() != self.left_answer_sha256:
            raise ValueError("left blind answer hash mismatch")
        if (
            hashlib.sha256(self.right_answer.encode("utf-8")).hexdigest()
            != self.right_answer_sha256
        ):
            raise ValueError("right blind answer hash mismatch")
        return self


class BlindReviewDecision(_StrictModel):
    pair_id: Identifier
    query_id: Identifier
    rater_key: Identifier
    naturalness_preference: PairwisePreference | None = None
    factual_completeness_preference: PairwisePreference | None = None


class BlindReviewState(_StrictModel):
    schema_version: Literal["cardrag.blind-review-state.v1"]
    packet_sha256: Sha256Hex
    pair_count: int = Field(ge=1)
    decisions: tuple[BlindReviewDecision, ...]

    @model_validator(mode="after")
    def state_has_exact_pair_coverage(self) -> Self:
        if len(self.decisions) != self.pair_count:
            raise ValueError("blind review state count mismatch")
        ids = [item.pair_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("blind review state has duplicate pairs")
        return self


@dataclass(frozen=True, slots=True)
class DraftDataset:
    manifest: DraftManifest
    records: tuple[GoldDraftRecord, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class BlindPacketDataset:
    manifest: BlindPacketManifest
    pairs: tuple[BlindPacketPair, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class _InventorySpan:
    span_id: str
    contract_revision_id: str
    issuer: str
    product_name: str
    product_lineage_id: str
    temporal_status: str
    effective_date: str
    node_type: str
    major_class: str
    heading: str
    page: int
    source_start: int
    source_end: int
    text_sha256: str
    text: str

    @property
    def identity(self) -> str:
        return f"{self.contract_revision_id}:{self.span_id}"

    def to_evidence(self) -> DraftEvidence:
        return DraftEvidence.model_validate(
            {
                "span_id": self.span_id,
                "contract_revision_id": self.contract_revision_id,
                "issuer": self.issuer,
                "product_name": self.product_name,
                "product_lineage_id": self.product_lineage_id,
                "temporal_status": self.temporal_status,
                "effective_date": self.effective_date,
                "node_type": self.node_type,
                "major_class": self.major_class,
                "heading": self.heading,
                "page": self.page,
                "source_start": self.source_start,
                "source_end": self.source_end,
                "text_sha256": self.text_sha256,
                "text": self.text,
            },
            strict=True,
        )


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _hash_descriptor(
    descriptor: int,
    *,
    maximum: int,
) -> tuple[str, int, tuple[int, int, int, int, int, int, int, int, int]]:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
        raise GoldReviewError("input_size_invalid")
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        block = os.pread(descriptor, min(1024 * 1024, before.st_size - offset), offset)
        if not block:
            raise GoldReviewError("input_changed_during_read")
        digest.update(block)
        offset += len(block)
    if os.pread(descriptor, 1, offset):
        raise GoldReviewError("input_changed_during_read")
    after = os.fstat(descriptor)
    identity = _file_identity(before)
    if offset != before.st_size or identity != _file_identity(after):
        raise GoldReviewError("input_changed_during_read")
    return digest.hexdigest(), offset, identity


@contextmanager
def _pinned_sqlite_database(
    path: Path,
) -> Iterator[tuple[sqlite3.Connection, str, int]]:
    """Hash and query one immutable SQLite inode through a retained descriptor."""

    absolute = _absolute_without_resolving(path)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise GoldReviewError("input_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise GoldReviewError("input_not_regular")
    if listed.st_size <= 0 or listed.st_size > MAX_DATABASE_BYTES:
        raise GoldReviewError("input_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise GoldReviewError("input_open_failed") from exc
    try:
        database_sha256, size_bytes, identity = _hash_descriptor(
            descriptor,
            maximum=MAX_DATABASE_BYTES,
        )
        if identity != _file_identity(listed):
            raise GoldReviewError("input_changed_during_read")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            yield connection, database_sha256, size_bytes
        finally:
            if connection is not None:
                connection.close()
            final_sha256, final_size, final_identity = _hash_descriptor(
                descriptor,
                maximum=MAX_DATABASE_BYTES,
            )
            try:
                current = absolute.lstat()
            except FileNotFoundError:
                raise GoldReviewError("input_changed_during_read") from None
            if (
                final_sha256 != database_sha256
                or final_size != size_bytes
                or final_identity != identity
                or _file_identity(current) != identity
            ):
                raise GoldReviewError("input_changed_during_read")
    finally:
        os.close(descriptor)


def _read_regular(path: Path, *, maximum: int = MAX_REVIEW_FILE_BYTES) -> bytes:
    absolute = _absolute_without_resolving(path)
    try:
        listed = absolute.lstat()
    except FileNotFoundError:
        raise GoldReviewError("input_missing") from None
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise GoldReviewError("input_not_regular")
    if listed.st_size <= 0 or listed.st_size > maximum:
        raise GoldReviewError("input_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise GoldReviewError("input_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GoldReviewError("input_not_regular")
        if before.st_size <= 0 or before.st_size > maximum:
            raise GoldReviewError("input_size_invalid")
        if _file_identity(listed) != _file_identity(before):
            raise GoldReviewError("input_changed_during_read")
        chunks: list[bytes] = []
        remaining = before.st_size
        size = 0
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise GoldReviewError("input_changed_during_read")
            if len(block) > maximum - size:
                raise GoldReviewError("input_size_invalid")
            chunks.append(block)
            size += len(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise GoldReviewError("input_changed_during_read")
        after = os.fstat(descriptor)
        try:
            current = absolute.lstat()
        except OSError:
            raise GoldReviewError("input_changed_during_read") from None
        identity = _file_identity(before)
        if (
            size != before.st_size
            or identity != _file_identity(after)
            or identity != _file_identity(current)
        ):
            raise GoldReviewError("input_changed_during_read")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _decode_json(data: bytes) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GoldReviewError("json_duplicate_key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise GoldReviewError("json_non_finite_number")

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except GoldReviewError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise GoldReviewError("json_invalid") from exc
    if not isinstance(value, dict):
        raise GoldReviewError("json_not_object")
    result = cast(dict[str, Any], value)
    if canonical_json_bytes(result) + b"\n" != data:
        raise GoldReviewError("json_not_canonical")
    return result


def _decode_jsonl(data: bytes) -> tuple[dict[str, Any], ...]:
    if not data.endswith(b"\n") or data.startswith(b"\xef\xbb\xbf"):
        raise GoldReviewError("jsonl_not_canonical_lines")
    records: list[dict[str, Any]] = []
    for raw in data.splitlines():
        if not raw or len(raw) > MAX_JSONL_LINE_BYTES:
            raise GoldReviewError("jsonl_line_invalid")
        record = _decode_json(raw + b"\n")
        records.append(record)
    if not records:
        raise GoldReviewError("jsonl_empty")
    return tuple(records)


def _json_file_bytes(model: BaseModel) -> bytes:
    return canonical_json_bytes(model) + b"\n"


def _jsonl_bytes(models: Sequence[BaseModel]) -> bytes:
    return b"".join(canonical_json_bytes(model) + b"\n" for model in models)


def _safe_parent(path: Path) -> Path:
    absolute = _absolute_without_resolving(path)
    parent = absolute.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = parent.lstat()
    except FileNotFoundError:
        raise GoldReviewError("output_parent_missing") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise GoldReviewError("output_parent_not_directory")
    return parent


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_temp(parent: Path, basename: str, data: bytes) -> Path:
    temp = parent / f".{basename}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temp, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise GoldReviewError("output_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temp.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    return temp


def _atomic_state_write(path: Path, data: bytes) -> None:
    parent = _safe_parent(path)
    absolute = _absolute_without_resolving(path)
    if os.path.lexists(absolute):
        metadata = absolute.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise GoldReviewError("state_target_not_regular")
    temp = _write_temp(parent, absolute.name, data)
    try:
        os.replace(temp, absolute)
        _fsync_directory(parent)
    finally:
        temp.unlink(missing_ok=True)


def _publish_validated(
    path: Path,
    data: bytes,
    *,
    validator: Callable[[Path], None],
) -> None:
    parent = _safe_parent(path)
    absolute = _absolute_without_resolving(path)
    if os.path.lexists(absolute):
        existing = _read_regular(absolute)
        if not hmac.compare_digest(existing, data):
            raise GoldReviewError("output_already_exists_different")
        validator(absolute)
        return
    temp = _write_temp(parent, absolute.name, data)
    try:
        validator(temp)
        try:
            os.link(temp, absolute, follow_symlinks=False)
        except FileExistsError:
            existing = _read_regular(absolute)
            if not hmac.compare_digest(existing, data):
                raise GoldReviewError("output_publish_race") from None
        _fsync_directory(parent)
    finally:
        temp.unlink(missing_ok=True)


def _load_draft(path: Path) -> DraftDataset:
    data = _read_regular(path)
    records = _decode_jsonl(data)
    try:
        manifest = DraftManifest.model_validate_json(
            canonical_json_bytes(records[0]),
            strict=True,
        )
        queries = tuple(
            GoldDraftRecord.model_validate_json(canonical_json_bytes(record), strict=True)
            for record in records[1:]
        )
    except ValidationError as exc:
        raise GoldReviewError("draft_schema_invalid") from exc
    if manifest.query_count != len(queries):
        raise GoldReviewError("draft_count_mismatch")
    if [item.ordinal for item in queries] != list(range(1, len(queries) + 1)):
        raise GoldReviewError("draft_order_invalid")
    ids = [item.query_id for item in queries]
    if len(ids) != len(set(ids)):
        raise GoldReviewError("draft_query_duplicate")
    counts = Counter(item.issuer for item in queries)
    if dict(sorted(counts.items())) != manifest.issuer_counts:
        raise GoldReviewError("draft_issuer_count_mismatch")
    if sum(item.proposed_gold.no_answer for item in queries) != manifest.no_answer_count:
        raise GoldReviewError("draft_no_answer_count_mismatch")
    return DraftDataset(manifest, queries, hashlib.sha256(data).hexdigest())


def _load_gold_state(path: Path, draft: DraftDataset) -> GoldReviewState:
    try:
        state = GoldReviewState.model_validate_json(
            canonical_json_bytes(_decode_json(_read_regular(path))),
            strict=True,
        )
    except ValidationError as exc:
        raise GoldReviewError("gold_review_state_invalid") from exc
    if state.draft_sha256 != draft.sha256 or state.query_count != len(draft.records):
        raise GoldReviewError("gold_review_state_binding_mismatch")
    if tuple(item.query_id for item in state.decisions) != tuple(
        item.query_id for item in draft.records
    ):
        raise GoldReviewError("gold_review_state_order_mismatch")
    for decision, record in zip(state.decisions, draft.records, strict=True):
        _validate_annotation_sources(decision.annotation, record)
    return state


def _initial_gold_state(draft: DraftDataset) -> GoldReviewState:
    return GoldReviewState(
        schema_version="cardrag.gold-review-state.v1",
        draft_sha256=draft.sha256,
        query_count=len(draft.records),
        decisions=tuple(
            GoldReviewDecision(
                query_id=record.query_id,
                status="pending",
                annotation=record.proposed_gold,
            )
            for record in draft.records
        ),
    )


def _load_or_create_gold_state(path: Path, draft: DraftDataset) -> GoldReviewState:
    if os.path.lexists(_absolute_without_resolving(path)):
        return _load_gold_state(path, draft)
    state = _initial_gold_state(draft)
    _atomic_state_write(path, _json_file_bytes(state))
    return state


def _load_blind_packet(path: Path) -> BlindPacketDataset:
    data = _read_regular(path)
    records = _decode_jsonl(data)
    try:
        manifest = BlindPacketManifest.model_validate_json(
            canonical_json_bytes(records[0]),
            strict=True,
        )
        pairs = tuple(
            BlindPacketPair.model_validate_json(canonical_json_bytes(record), strict=True)
            for record in records[1:]
        )
    except ValidationError as exc:
        raise GoldReviewError("blind_packet_schema_invalid") from exc
    if manifest.pair_count != len(pairs):
        raise GoldReviewError("blind_packet_count_mismatch")
    pair_ids = [pair.pair_id for pair in pairs]
    if len(pair_ids) != len(set(pair_ids)):
        raise GoldReviewError("blind_packet_pair_duplicate")
    counts = Counter(pair.query_id for pair in pairs)
    if len(counts) != manifest.query_count or set(counts.values()) != {manifest.ratings_per_query}:
        raise GoldReviewError("blind_packet_query_coverage_mismatch")
    if {pair.rater_key for pair in pairs} != set(manifest.rater_keys):
        raise GoldReviewError("blind_packet_rater_coverage_mismatch")
    query_ids = set(counts)
    query_raters = {(pair.query_id, pair.rater_key) for pair in pairs}
    expected_query_raters = {
        (query_id, rater_key) for query_id in query_ids for rater_key in manifest.rater_keys
    }
    if len(query_raters) != len(pairs) or query_raters != expected_query_raters:
        raise GoldReviewError("blind_packet_query_rater_coverage_mismatch")
    return BlindPacketDataset(manifest, pairs, hashlib.sha256(data).hexdigest())


def _initial_blind_state(packet: BlindPacketDataset) -> BlindReviewState:
    return BlindReviewState(
        schema_version="cardrag.blind-review-state.v1",
        packet_sha256=packet.sha256,
        pair_count=len(packet.pairs),
        decisions=tuple(
            BlindReviewDecision(
                pair_id=pair.pair_id,
                query_id=pair.query_id,
                rater_key=pair.rater_key,
            )
            for pair in packet.pairs
        ),
    )


def _load_blind_state(path: Path, packet: BlindPacketDataset) -> BlindReviewState:
    try:
        state = BlindReviewState.model_validate_json(
            canonical_json_bytes(_decode_json(_read_regular(path))),
            strict=True,
        )
    except ValidationError as exc:
        raise GoldReviewError("blind_review_state_invalid") from exc
    if state.packet_sha256 != packet.sha256 or state.pair_count != len(packet.pairs):
        raise GoldReviewError("blind_review_state_binding_mismatch")
    expected = tuple((pair.pair_id, pair.query_id, pair.rater_key) for pair in packet.pairs)
    actual = tuple((item.pair_id, item.query_id, item.rater_key) for item in state.decisions)
    if actual != expected:
        raise GoldReviewError("blind_review_state_order_mismatch")
    return state


def _load_or_create_blind_state(path: Path, packet: BlindPacketDataset) -> BlindReviewState:
    if os.path.lexists(_absolute_without_resolving(path)):
        return _load_blind_state(path, packet)
    state = _initial_blind_state(packet)
    _atomic_state_write(path, _json_file_bytes(state))
    return state


def _validate_annotation_sources(annotation: GoldQuery, record: GoldDraftRecord) -> None:
    if annotation.query_id != record.query_id:
        raise ValueError("annotation query identity changed")
    if f"issuer:{record.issuer}" not in annotation.slices:
        raise ValueError("annotation issuer slice changed")
    if annotation.no_answer:
        if record.evidence:
            raise ValueError("positive-evidence draft cannot become no-answer")
        return
    if not record.evidence:
        raise ValueError("no-answer draft cannot become positive")
    allowed = {
        evidence.span_id: (
            evidence.contract_revision_id,
            evidence.page,
            evidence.source_start,
            evidence.source_end,
            evidence.text_sha256,
        )
        for evidence in record.evidence
    }
    if len(allowed) != len(record.evidence):
        raise ValueError("draft evidence IDs are not unique")
    for span in annotation.spans:
        actual = (
            span.contract_revision_id,
            span.page,
            span.source_start,
            span.source_end,
            span.text_sha256,
        )
        if allowed.get(span.span_id) != actual:
            raise ValueError("annotation span is not an exact draft source candidate")
    if {span.contract_revision_id for span in annotation.spans} != {
        contract.contract_revision_id for contract in annotation.contracts
    }:
        raise ValueError("annotation contracts must be exactly its source contracts")


def _stable_rank(seed: int, *parts: str) -> str:
    payload = "\x1f".join((str(seed), *parts)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _clean_one_line(text: str, *, maximum: int) -> str:
    result = " ".join(text.split())
    if _CONTROL.search(result):
        result = _CONTROL.sub(" ", result)
        result = " ".join(result.split())
    return result[:maximum].strip()


def _inventory_rows(
    database: Path,
) -> tuple[str, str, str, int, str, tuple[_InventorySpan, ...]]:
    try:
        with _pinned_sqlite_database(database) as (
            connection,
            database_sha256,
            size_bytes,
        ):
            metadata = validate_schema_v5(connection)
            rows = connection.execute(
                """SELECT l.issuer,l.name AS product_name,l.product_lineage_id,
                          r.contract_revision_id,r.temporal_status,r.effective_date,
                          n.node_id,n.node_type,n.major_class,coalesce(n.raw_heading,''),
                          s.page,s.source_start,s.source_end,s.text_sha256,
                          p.text AS page_text,p.text_sha256 AS page_text_sha256
                     FROM node_spans AS s
                     JOIN structure_nodes AS n
                       ON n.node_id=s.node_id
                      AND n.contract_revision_id=s.contract_revision_id
                     JOIN contract_revisions AS r
                       ON r.contract_revision_id=s.contract_revision_id
                     JOIN product_lineages AS l
                       ON l.product_lineage_id=r.product_lineage_id
                     JOIN document_pages AS p
                       ON p.contract_revision_id=s.contract_revision_id AND p.page=s.page
                    WHERE s.is_canonical=1
                      AND n.node_type!='ROOT'
                    ORDER BY l.issuer,r.contract_revision_id,n.ordinal,s.span_ordinal,n.node_id"""
            ).fetchall()
    except (sqlite3.Error, ServingDatabaseV5Error) as exc:
        raise GoldReviewError("serving_database_invalid") from exc

    inventory: list[_InventorySpan] = []
    seen_nodes: set[tuple[str, str]] = set()
    for row in rows:
        issuer = str(row[0]).lower()
        if issuer not in ISSUERS:
            continue
        node_identity = (str(row[3]), str(row[6]))
        if node_identity in seen_nodes:
            continue
        page_text = str(row[14])
        if hashlib.sha256(page_text.encode("utf-8")).hexdigest() != str(row[15]):
            raise GoldReviewError("document_page_hash_mismatch")
        start, end = int(row[11]), int(row[12])
        if start < 0 or end <= start or end > len(page_text):
            raise GoldReviewError("node_span_bounds_invalid")
        text = page_text[start:end]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != str(row[13]):
            raise GoldReviewError("node_span_hash_mismatch")
        if not text.strip() or len(text) > 65_536:
            continue
        try:
            item = _InventorySpan(
                span_id=str(row[6]),
                contract_revision_id=str(row[3]),
                issuer=issuer,
                product_name=_clean_one_line(str(row[1]), maximum=1024),
                product_lineage_id=str(row[2]),
                temporal_status=str(row[4]),
                effective_date=str(row[5]),
                node_type=str(row[7]),
                major_class=str(row[8]),
                heading=_clean_one_line(str(row[9]), maximum=4096),
                page=int(row[10]),
                source_start=start,
                source_end=end,
                text_sha256=str(row[13]),
                text=text,
            )
            item.to_evidence()
        except (ValidationError, ValueError) as exc:
            raise GoldReviewError("inventory_row_invalid") from exc
        seen_nodes.add(node_identity)
        inventory.append(item)
    if not inventory:
        raise GoldReviewError("inventory_empty")
    inventory_sha256 = canonical_sha256(
        [
            {
                "contract_revision_id": item.contract_revision_id,
                "effective_date": item.effective_date,
                "issuer": item.issuer,
                "major_class": item.major_class,
                "node_type": item.node_type,
                "page": item.page,
                "product_lineage_id": item.product_lineage_id,
                "source_end": item.source_end,
                "source_start": item.source_start,
                "span_id": item.span_id,
                "temporal_status": item.temporal_status,
                "text_sha256": item.text_sha256,
            }
            for item in inventory
        ]
    )
    return (
        metadata.generation_id,
        metadata.corpus_sha256,
        database_sha256,
        size_bytes,
        inventory_sha256,
        tuple(inventory),
    )


def _slice_match(item: _InventorySpan, slice_name: str) -> int:
    haystack = f"{item.heading}\n{item.text}".lower()
    score = sum(3 for keyword in _KEYWORDS.get(slice_name, ()) if keyword.lower() in haystack)
    if slice_name == "table" and item.node_type in {"TABLE", "TABLE_ROW"}:
        score += 20
    if slice_name == "footnote" and item.node_type == "FOOTNOTE":
        score += 20
    if slice_name == "benefit" and item.major_class == "BENEFIT":
        score += 10
    if slice_name == "common_notice" and item.major_class == "NOTICE":
        score += 10
    if slice_name == "current_history" and item.temporal_status == "current":
        score += 4
    if slice_name in {"limit", "frequency", "minimum_payment", "annual_fee", "foreign_fee"}:
        score += 8 if _NUMERIC_FACT.search(item.text) else 0
    return score


def _pick_primary(
    values: Sequence[_InventorySpan],
    *,
    slice_name: str,
    seed: int,
    ordinal: int,
    used: set[str],
) -> _InventorySpan:
    ranked = sorted(
        values,
        key=lambda item: (
            item.identity in used,
            -_slice_match(item, slice_name),
            _stable_rank(seed, slice_name, str(ordinal), item.identity),
            item.identity,
        ),
    )
    if not ranked:
        raise GoldReviewError("issuer_inventory_empty")
    selected = ranked[0]
    used.add(selected.identity)
    return selected


def _pick_companion(
    values: Sequence[_InventorySpan],
    primary: _InventorySpan,
    *,
    purpose: str,
    seed: int,
    ordinal: int,
) -> _InventorySpan | None:
    eligible: list[_InventorySpan] = []
    for item in values:
        if item.span_id == primary.span_id:
            continue
        if purpose == "comparison" and item.product_lineage_id == primary.product_lineage_id:
            continue
        if purpose == "cross_page" and (
            item.contract_revision_id != primary.contract_revision_id or item.page == primary.page
        ):
            continue
        if purpose == "condition" and item.contract_revision_id != primary.contract_revision_id:
            continue
        if purpose == "history" and (
            item.product_lineage_id != primary.product_lineage_id
            or item.contract_revision_id == primary.contract_revision_id
        ):
            continue
        eligible.append(item)
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            -_slice_match(item, "benefit" if purpose == "condition" else purpose),
            _stable_rank(seed, purpose, str(ordinal), primary.identity, item.identity),
            item.identity,
        ),
    )


def _roles_for(
    slice_name: str, item: _InventorySpan, *, companion: bool = False
) -> tuple[str, ...]:
    roles: set[str] = set()
    if slice_name in {
        "benefit",
        "earning",
        "discount",
        "cashback",
        "discovery_recommendation",
        "comparison",
    } or (companion and slice_name == "limit"):
        roles.add("benefit")
    if (
        slice_name
        in {
            "performance",
            "limit",
            "frequency",
            "minimum_payment",
            "issuance_condition",
            "grace_period",
        }
        and not companion
    ):
        roles.add("condition")
    if slice_name in {"exclusion", "negation", "exception", "hard_negative"}:
        roles.add("exclusion")
    if slice_name in {"annual_fee", "foreign_fee", "common_notice"}:
        roles.add("notice")
    if slice_name == "current_history":
        roles.add("revision")
    if _NUMERIC_FACT.search(item.text):
        roles.add("numeric")
    if not roles:
        roles.add("notice" if item.major_class == "NOTICE" else "other")
    return tuple(sorted(roles))


def _question_for(
    slice_name: str,
    primary: _InventorySpan,
    companion: _InventorySpan | None,
) -> str:
    topic = primary.heading or "해당 항목"
    product = primary.product_name
    templates: Mapping[str, str] = {
        "benefit": f"{product}의 {topic} 혜택과 적용 조건은 무엇인가요?",
        "earning": f"{product}의 {topic} 적립 기준은 무엇인가요?",
        "discount": f"{product}의 {topic} 할인 조건과 한도는 무엇인가요?",
        "cashback": f"{product}의 {topic} 캐시백 제공 조건은 무엇인가요?",
        "performance": f"{product}의 {topic} 전월 실적 조건은 어떻게 계산되나요?",
        "exclusion": f"{product}의 {topic} 혜택에서 제외되는 이용은 무엇인가요?",
        "limit": f"{product}의 {topic} 혜택 한도와 그 적용 조건을 함께 설명해 주세요.",
        "frequency": f"{product}의 {topic} 제공 횟수 제한은 어떻게 되나요?",
        "minimum_payment": f"{product}의 {topic} 혜택을 받기 위한 최소 결제 조건은 무엇인가요?",
        "annual_fee": f"{product}의 연회비와 {topic} 관련 비용 조건은 무엇인가요?",
        "issuance_condition": f"{product}의 {topic} 발급 또는 신청 조건은 무엇인가요?",
        "foreign_fee": f"{product}의 {topic} 해외 이용 수수료 조건은 무엇인가요?",
        "negation": f"{product}의 {topic}에서 제공되지 않거나 인정되지 않는 경우는 무엇인가요?",
        "exception": f"{product}의 {topic} 일반 기준에 대한 예외는 무엇인가요?",
        "grace_period": f"{product}의 {topic} 적용 기간 또는 유예 조건은 무엇인가요?",
        "table": f"{product}의 {topic} 표에 적힌 조건을 항목별로 설명해 주세요.",
        "footnote": f"{product}의 {topic} 각주가 본문 혜택에 추가하는 조건은 무엇인가요?",
        "cross_page": (
            f"{product}의 {topic} 혜택과 다른 페이지에 이어지는 조건을 함께 설명해 주세요."
        ),
        "common_notice": f"{product} 이용 시 {topic} 공통 유의사항은 무엇인가요?",
        "hard_negative": (
            f"{product}의 {topic}와 비슷하지만 실제로는 적용 대상에서 제외되는 경우는 무엇인가요?"
        ),
        "current_history": f"{product}의 {topic}에 현재 적용되는 개정 기준은 무엇인가요?",
        "product_specific": f"{product}에만 적용되는 {topic} 조건은 무엇인가요?",
        "discovery_recommendation": (
            f"{topic} 조건을 원하는 이용자에게 적합한 상품과 적용 조건을 알려 주세요."
        ),
        "long": (
            f"{product}의 {topic} 혜택을 이용하려고 합니다. 제공 내용뿐 아니라 실적, 한도, "
            "제외 또는 유의 조건까지 원문 근거에 맞춰 빠짐없이 설명해 주세요."
        ),
    }
    if slice_name == "comparison" and companion is not None:
        value = (
            f"{primary.product_name}과 {companion.product_name}의 {topic} 관련 조건을 "
            "원문 기준으로 비교해 주세요."
        )
    else:
        value = templates.get(slice_name, f"{product}의 {topic} 조건은 무엇인가요?")
    result = _clean_one_line(value, maximum=4096)
    if not result:
        raise GoldReviewError("draft_question_empty")
    return result


def _no_answer_question(issuer: str, index: int) -> str:
    return (
        f"현재 자료에서 {issuer.upper()}의 gold 검증용 가상 상품 {index:02d}가 제공하는 "
        "반려동물 의료비 보험 한도는 얼마인가요?"
    )


def _build_positive_query(
    *,
    query_id: str,
    issuer: str,
    slice_name: str,
    primary: _InventorySpan,
    companion: _InventorySpan | None,
) -> tuple[GoldQuery, tuple[DraftEvidence, ...]]:
    selected = [primary]
    if companion is not None:
        selected.append(companion)
    # Node IDs are evaluation span identities and therefore must remain unique.
    unique: dict[str, _InventorySpan] = {item.span_id: item for item in selected}
    selected = list(unique.values())
    spans: list[GoldSpan] = []
    for position, item in enumerate(selected):
        roles = _roles_for(slice_name, item, companion=position > 0)
        spans.append(
            GoldSpan(
                span_id=item.span_id,
                contract_revision_id=item.contract_revision_id,
                page=item.page,
                source_start=item.source_start,
                source_end=item.source_end,
                text_sha256=item.text_sha256,
                relevance=3 if position == 0 else 2,
                roles=cast(tuple[EvidenceRole, ...], roles),
            )
        )
    contracts = tuple(
        GoldContract(contract_revision_id=contract_id, relevance=3 if index == 0 else 2)
        for index, contract_id in enumerate(
            dict.fromkeys(item.contract_revision_id for item in selected)
        )
    )
    slices = {slice_name, f"issuer:{issuer}"}
    if any(item.major_class == "NOTICE" for item in selected) or slice_name == "common_notice":
        slices.add("major:notice")
    else:
        slices.add("major:benefit")
    if slice_name in {"limit", "annual_fee", "foreign_fee", "frequency", "minimum_payment"}:
        slices.add("benefit")
    facts = tuple(
        dict.fromkeys(
            fact.replace(" ", "") for item in selected for fact in _NUMERIC_FACT.findall(item.text)
        )
    )[:8]
    condition_groups: tuple[dict[str, Any], ...] = ()
    if slice_name == "limit" and len(spans) >= 2:
        # The second span is explicitly a benefit companion, while the first is
        # condition/numeric evidence.  Human review must confirm the relation.
        condition_groups = (
            {"at_k": 10, "span_ids": tuple(sorted((spans[0].span_id, spans[1].span_id)))},
        )
    expected_revisions = (primary.contract_revision_id,) if slice_name == "current_history" else ()
    if expected_revisions and "revision" not in spans[0].roles:
        raise GoldReviewError("draft_revision_role_missing")
    try:
        gold = GoldQuery.model_validate(
            {
                "schema_version": GOLD_SCHEMA_VERSION,
                "query_id": query_id,
                "question": _question_for(slice_name, primary, companion),
                "slices": tuple(sorted(slices)),
                "contracts": contracts,
                "spans": tuple(spans),
                "condition_groups": condition_groups,
                "expected_numeric_facts": facts,
                "expected_revision_ids": expected_revisions,
                "no_answer": False,
                "high_risk": bool(facts or condition_groups or expected_revisions),
            }
        )
    except ValidationError as exc:
        raise GoldReviewError("draft_gold_invalid") from exc
    return gold, tuple(item.to_evidence() for item in selected)


def build_draft(
    database: Path,
    *,
    count: int = DEFAULT_QUERY_COUNT,
    no_answer_count: int = DEFAULT_NO_ANSWER_COUNT,
    seed: int = DEFAULT_SEED,
) -> tuple[DraftManifest, tuple[GoldDraftRecord, ...]]:
    """Build a deterministic, corpus-only draft; no label is auto-approved."""

    if not MIN_RELEASE_QUERIES <= count <= MAX_RELEASE_QUERIES:
        raise GoldReviewError("draft_count_invalid")
    if not len(ISSUERS) <= no_answer_count < count:
        raise GoldReviewError("draft_no_answer_count_invalid")
    if not 0 <= seed <= 2**63 - 1:
        raise GoldReviewError("draft_seed_invalid")
    (
        generation_id,
        corpus_sha256,
        database_sha256,
        size_bytes,
        inventory_sha256,
        inventory,
    ) = _inventory_rows(database)
    by_issuer: dict[str, tuple[_InventorySpan, ...]] = {}
    for issuer in ISSUERS:
        values = tuple(item for item in inventory if item.issuer == issuer)
        if not values:
            raise GoldReviewError("issuer_inventory_empty")
        by_issuer[issuer] = values

    issuer_targets = {issuer: count // len(ISSUERS) for issuer in ISSUERS}
    for issuer in ISSUERS[: count % len(ISSUERS)]:
        issuer_targets[issuer] += 1
    no_answer_targets = {issuer: no_answer_count // len(ISSUERS) for issuer in ISSUERS}
    for issuer in ISSUERS[: no_answer_count % len(ISSUERS)]:
        no_answer_targets[issuer] += 1
    if any(no_answer_targets[key] >= issuer_targets[key] for key in ISSUERS):
        raise GoldReviewError("draft_issuer_positive_coverage_missing")

    records: list[GoldDraftRecord] = []
    used: set[str] = set()
    positive_ordinal = 0
    no_answer_serial = 0
    for issuer in ISSUERS:
        positive_target = issuer_targets[issuer] - no_answer_targets[issuer]
        values = by_issuer[issuer]
        for _ in range(positive_target):
            ordinal = len(records) + 1
            slice_name = _DOMAIN_SLICES[positive_ordinal % len(_DOMAIN_SLICES)]
            positive_ordinal += 1
            primary = _pick_primary(
                values,
                slice_name=slice_name,
                seed=seed,
                ordinal=ordinal,
                used=used,
            )
            purpose: str | None = None
            if slice_name == "comparison":
                purpose = "comparison"
            elif slice_name == "cross_page":
                purpose = "cross_page"
            elif slice_name == "limit":
                purpose = "condition"
            elif slice_name == "current_history":
                purpose = "history"
            companion = (
                _pick_companion(
                    values,
                    primary,
                    purpose=purpose,
                    seed=seed,
                    ordinal=ordinal,
                )
                if purpose is not None
                else None
            )
            if purpose is not None and companion is None:
                raise GoldReviewError(f"draft_{purpose}_source_missing")
            query_id = f"gold-{ordinal:03d}"
            gold, evidence = _build_positive_query(
                query_id=query_id,
                issuer=issuer,
                slice_name=slice_name,
                primary=primary,
                companion=companion,
            )
            records.append(
                GoldDraftRecord(
                    schema_version="cardrag.gold-draft-query.v1",
                    ordinal=ordinal,
                    query_id=query_id,
                    issuer=cast(Any, issuer),
                    primary_slice=slice_name,
                    selection_basis="corpus_inventory_only",
                    proposed_gold=gold,
                    evidence=evidence,
                )
            )
        for _ in range(no_answer_targets[issuer]):
            ordinal = len(records) + 1
            no_answer_serial += 1
            query_id = f"gold-{ordinal:03d}"
            gold = GoldQuery(
                schema_version="cardrag.gold-query.v1",
                query_id=query_id,
                question=_no_answer_question(issuer, no_answer_serial),
                slices=tuple(sorted((f"issuer:{issuer}", "hard_negative", "no_answer"))),
                no_answer=True,
            )
            records.append(
                GoldDraftRecord(
                    schema_version="cardrag.gold-draft-query.v1",
                    ordinal=ordinal,
                    query_id=query_id,
                    issuer=cast(Any, issuer),
                    primary_slice="no_answer",
                    selection_basis="corpus_inventory_only",
                    proposed_gold=gold,
                    evidence=(),
                )
            )

    manifest = DraftManifest(
        schema_version="cardrag.gold-draft-artifact.v1",
        algorithm="corpus-inventory-stratified.v1",
        generation_id=generation_id,
        corpus_sha256=corpus_sha256,
        serving_database_sha256=database_sha256,
        serving_database_size_bytes=size_bytes,
        inventory_sha256=inventory_sha256,
        seed=seed,
        query_count=len(records),
        no_answer_count=no_answer_count,
        issuer_counts=dict(sorted(issuer_targets.items())),
        required_slices=tuple(sorted(REQUIRED_RELEASE_SLICES)),
        candidate_performance_selection=False,
        provider_calls=False,
    )
    all_slices = {slice_name for record in records for slice_name in record.proposed_gold.slices}
    if not REQUIRED_RELEASE_SLICES.issubset(all_slices):
        raise GoldReviewError("draft_required_slice_missing")
    if (
        not any(record.proposed_gold.condition_groups for record in records)
        or not any(record.proposed_gold.expected_revision_ids for record in records)
        or not any(
            record.proposed_gold.high_risk and record.proposed_gold.expected_numeric_facts
            for record in records
        )
    ):
        raise GoldReviewError("draft_high_risk_coverage_missing")
    return manifest, tuple(records)


def write_draft(
    database: Path,
    output: Path,
    state_path: Path,
    *,
    count: int = DEFAULT_QUERY_COUNT,
    no_answer_count: int = DEFAULT_NO_ANSWER_COUNT,
    seed: int = DEFAULT_SEED,
) -> DraftDataset:
    manifest, records = build_draft(
        database,
        count=count,
        no_answer_count=no_answer_count,
        seed=seed,
    )
    payload = _jsonl_bytes((manifest, *records))

    def validate_draft(staged: Path) -> None:
        _load_draft(staged)

    _publish_validated(output, payload, validator=validate_draft)
    draft = _load_draft(output)
    _load_or_create_gold_state(state_path, draft)
    return draft


def seal_gold(draft_path: Path, state_path: Path, output: Path) -> str:
    draft = _load_draft(draft_path)
    state = _load_gold_state(state_path, draft)
    if any(item.status != "approved" for item in state.decisions):
        raise GoldReviewError("gold_review_incomplete")
    annotations: list[GoldQuery] = []
    for decision, record in zip(state.decisions, draft.records, strict=True):
        try:
            _validate_annotation_sources(decision.annotation, record)
        except ValueError as exc:
            raise GoldReviewError("gold_review_source_binding_invalid") from exc
        annotations.append(decision.annotation)
    payload = _jsonl_bytes(annotations)

    def validate_release(path: Path) -> None:
        try:
            dataset = load_gold_jsonl(path, release_gate=True)
        except EvaluationError as exc:
            raise GoldReviewError("sealed_gold_release_validation_failed") from exc
        if dataset.sha256 != hashlib.sha256(payload).hexdigest():
            raise GoldReviewError("sealed_gold_hash_mismatch")

    _publish_validated(output, payload, validator=validate_release)
    return hashlib.sha256(payload).hexdigest()


def _validate_run_bindings(
    gold_path: Path,
    baseline_path: Path,
    candidate_path: Path,
) -> tuple[Any, RunDataset, RunDataset]:
    try:
        gold = load_gold_jsonl(gold_path, release_gate=True)
        baseline = load_run_jsonl(baseline_path, lane="v109_baseline")
        candidate = load_run_jsonl(candidate_path, lane="qwen_structure_exact")
    except EvaluationError as exc:
        raise GoldReviewError("blind_source_artifact_invalid") from exc
    for dataset in (baseline, candidate):
        if dataset.manifest.gold_sha256 != gold.sha256:
            raise GoldReviewError("blind_run_gold_binding_mismatch")
        if dataset.manifest.query_count != len(gold.queries):
            raise GoldReviewError("blind_run_count_mismatch")
        if tuple(item.query_id for item in dataset.results) != tuple(
            item.query_id for item in gold.queries
        ):
            raise GoldReviewError("blind_run_query_order_mismatch")
    return gold, baseline, candidate


def _candidate_left_map(
    query_ids: Sequence[str],
    rater_keys: Sequence[str],
    *,
    seed: int,
    source_run_sha256s: Sequence[str],
) -> dict[tuple[str, str], bool]:
    if len(query_ids) % 2:
        raise GoldReviewError("blind_exact_balance_requires_even_queries")
    result: dict[tuple[str, str], bool] = {}
    binding = "|".join(sorted(source_run_sha256s))
    for rater_key in rater_keys:
        ordered = sorted(
            query_ids,
            key=lambda query_id: (
                _stable_rank(seed, binding, rater_key, query_id),
                query_id,
            ),
        )
        left = set(ordered[: len(ordered) // 2])
        for query_id in query_ids:
            result[(rater_key, query_id)] = query_id in left
    return result


def prepare_blind(
    gold_path: Path,
    baseline_path: Path,
    candidate_path: Path,
    packet_path: Path,
    state_path: Path,
    *,
    rater_keys: Sequence[str] = ("anonymous-rater-01",),
    seed: int = DEFAULT_SEED,
) -> BlindPacketDataset:
    if not rater_keys or len(rater_keys) > MAX_BLIND_RATINGS_PER_QUERY:
        raise GoldReviewError("blind_rater_count_invalid")
    if len(set(rater_keys)) != len(rater_keys):
        raise GoldReviewError("blind_rater_duplicate")
    gold, baseline, candidate = _validate_run_bindings(
        gold_path,
        baseline_path,
        candidate_path,
    )
    source_hashes = tuple(sorted((baseline.sha256, candidate.sha256)))
    query_ids = tuple(item.query_id for item in gold.queries)
    positions = _candidate_left_map(
        query_ids,
        rater_keys,
        seed=seed,
        source_run_sha256s=source_hashes,
    )
    baseline_by_id = {item.query_id: item for item in baseline.results}
    candidate_by_id = {item.query_id: item for item in candidate.results}
    pairs: list[BlindPacketPair] = []
    for rater_index, rater_key in enumerate(rater_keys, start=1):
        for query_index, query in enumerate(gold.queries, start=1):
            baseline_answer = baseline_by_id[query.query_id].answer.text
            candidate_answer = candidate_by_id[query.query_id].answer.text
            candidate_left = positions[(rater_key, query.query_id)]
            left, right = (
                (candidate_answer, baseline_answer)
                if candidate_left
                else (baseline_answer, candidate_answer)
            )
            pairs.append(
                BlindPacketPair(
                    schema_version="cardrag.blind-review-pair.v1",
                    pair_id=f"pair-{rater_index:02d}-{query_index:03d}",
                    query_id=query.query_id,
                    rater_key=rater_key,
                    question=query.question,
                    left_answer=left,
                    left_answer_sha256=hashlib.sha256(left.encode("utf-8")).hexdigest(),
                    right_answer=right,
                    right_answer_sha256=hashlib.sha256(right.encode("utf-8")).hexdigest(),
                )
            )
    manifest = BlindPacketManifest(
        schema_version="cardrag.blind-review-packet.v1",
        presentation_protocol="anonymous-a-b.v1",
        rubric_id="cardrag.blind-rubric.naturalness-factual-completeness.v1",
        gold_sha256=gold.sha256,
        source_run_sha256s=cast(tuple[str, str], source_hashes),
        assignment_seed=seed,
        assignment_algorithm="sha256-balanced-per-rater.v1",
        query_count=len(gold.queries),
        ratings_per_query=len(rater_keys),
        pair_count=len(pairs),
        rater_keys=tuple(rater_keys),
        lane_identity_exposed_to_raters=False,
    )
    payload = _jsonl_bytes((manifest, *pairs))

    def validate_packet(staged: Path) -> None:
        _load_blind_packet(staged)

    _publish_validated(packet_path, payload, validator=validate_packet)
    packet = _load_blind_packet(packet_path)
    _load_or_create_blind_state(state_path, packet)
    return packet


def seal_blind(
    gold_path: Path,
    baseline_path: Path,
    candidate_path: Path,
    packet_path: Path,
    state_path: Path,
    output: Path,
) -> str:
    gold, baseline, candidate = _validate_run_bindings(
        gold_path,
        baseline_path,
        candidate_path,
    )
    packet = _load_blind_packet(packet_path)
    state = _load_blind_state(state_path, packet)
    if packet.manifest.gold_sha256 != gold.sha256 or packet.manifest.source_run_sha256s != tuple(
        sorted((baseline.sha256, candidate.sha256))
    ):
        raise GoldReviewError("blind_packet_source_binding_mismatch")
    if any(
        item.naturalness_preference is None or item.factual_completeness_preference is None
        for item in state.decisions
    ):
        raise GoldReviewError("blind_review_incomplete")
    query_ids = tuple(item.query_id for item in gold.queries)
    expected_pair_identity = tuple(
        (f"pair-{rater_index:02d}-{query_index:03d}", query.query_id, rater_key)
        for rater_index, rater_key in enumerate(packet.manifest.rater_keys, start=1)
        for query_index, query in enumerate(gold.queries, start=1)
    )
    actual_pair_identity = tuple(
        (pair.pair_id, pair.query_id, pair.rater_key) for pair in packet.pairs
    )
    if actual_pair_identity != expected_pair_identity:
        raise GoldReviewError("blind_packet_pair_identity_mismatch")
    questions = {query.query_id: query.question for query in gold.queries}
    if any(pair.question != questions[pair.query_id] for pair in packet.pairs):
        raise GoldReviewError("blind_packet_question_binding_mismatch")
    positions = _candidate_left_map(
        query_ids,
        packet.manifest.rater_keys,
        seed=packet.manifest.assignment_seed,
        source_run_sha256s=packet.manifest.source_run_sha256s,
    )
    baseline_by_id = {item.query_id: item for item in baseline.results}
    candidate_by_id = {item.query_id: item for item in candidate.results}
    ratings: list[BlindPairwiseRating] = []
    for pair, decision in zip(packet.pairs, state.decisions, strict=True):
        candidate_left = positions[(pair.rater_key, pair.query_id)]
        baseline_hash = hashlib.sha256(
            baseline_by_id[pair.query_id].answer.text.encode("utf-8")
        ).hexdigest()
        candidate_hash = hashlib.sha256(
            candidate_by_id[pair.query_id].answer.text.encode("utf-8")
        ).hexdigest()
        expected_hashes = (
            (candidate_hash, baseline_hash) if candidate_left else (baseline_hash, candidate_hash)
        )
        if (pair.left_answer_sha256, pair.right_answer_sha256) != expected_hashes:
            raise GoldReviewError("blind_packet_answer_binding_mismatch")
        naturalness = cast(PairwisePreference, decision.naturalness_preference)
        completeness = cast(PairwisePreference, decision.factual_completeness_preference)
        if baseline_hash == candidate_hash and (naturalness != "tie" or completeness != "tie"):
            raise GoldReviewError("blind_identical_answer_preference_invalid")
        ratings.append(
            BlindPairwiseRating(
                schema_version="cardrag.blind-pairwise-rating.v1",
                pair_id=pair.pair_id,
                query_id=pair.query_id,
                rater_key=pair.rater_key,
                candidate_position="left" if candidate_left else "right",
                left_answer_sha256=pair.left_answer_sha256,
                right_answer_sha256=pair.right_answer_sha256,
                naturalness_preference=naturalness,
                factual_completeness_preference=completeness,
            )
        )
    manifest = BlindEvaluationManifest(
        schema_version="cardrag.blind-evaluation-artifact.v1",
        gold_sha256=gold.sha256,
        baseline_lane="v109_baseline",
        baseline_run_sha256=baseline.sha256,
        candidate_lane="qwen_structure_exact",
        candidate_run_sha256=candidate.sha256,
        query_count=len(gold.queries),
        ratings_per_query=packet.manifest.ratings_per_query,
        pair_count=len(ratings),
        presentation_protocol="anonymous-a-b.v1",
        rubric_id="cardrag.blind-rubric.naturalness-factual-completeness.v1",
        lane_identity_exposed_to_raters=False,
    )
    payload = _jsonl_bytes((manifest, *ratings))

    def validate_blind(path: Path) -> None:
        try:
            dataset = load_blind_evaluation_jsonl(path)
        except EvaluationError as exc:
            raise GoldReviewError("sealed_blind_validation_failed") from exc
        if dataset.sha256 != hashlib.sha256(payload).hexdigest():
            raise GoldReviewError("sealed_blind_hash_mismatch")

    _publish_validated(output, payload, validator=validate_blind)
    return hashlib.sha256(payload).hexdigest()


class _ReviewController:
    def public_payload(self) -> dict[str, Any]:
        raise NotImplementedError

    def apply(self, payload: Mapping[str, Any]) -> None:
        raise NotImplementedError


class _GoldController(_ReviewController):
    def __init__(self, draft: DraftDataset, state_path: Path) -> None:
        self._draft = draft
        self._state_path = state_path
        self._state = _load_or_create_gold_state(state_path, draft)
        self._lock = threading.Lock()

    def public_payload(self) -> dict[str, Any]:
        with self._lock:
            decisions = {item.query_id: item for item in self._state.decisions}
            return {
                "mode": "gold",
                "rubric": (
                    "공개 PDF/OCR의 정확한 페이지·문자 범위·해시, 질문 유용성, slice, 숫자와 "
                    "개정·조건 관계를 직접 확인한 뒤에만 승인하세요. no-answer는 전 corpus에서 "
                    "답이 없음을 별도로 확인해야 합니다."
                ),
                "items": [
                    {
                        "query_id": record.query_id,
                        "status": decisions[record.query_id].status,
                        "annotation": decisions[record.query_id].annotation.model_dump(mode="json"),
                        "evidence": [item.model_dump(mode="json") for item in record.evidence],
                    }
                    for record in self._draft.records
                ],
            }

    def apply(self, payload: Mapping[str, Any]) -> None:
        if set(payload) != {"query_id", "status", "annotation"}:
            raise GoldReviewError("review_request_fields_invalid")
        query_id = payload.get("query_id")
        status_value = payload.get("status")
        if not isinstance(query_id, str) or status_value not in {"pending", "approved", "rejected"}:
            raise GoldReviewError("review_request_invalid")
        try:
            annotation = GoldQuery.model_validate_json(
                canonical_json_bytes(payload.get("annotation")),
                strict=True,
            )
        except ValidationError as exc:
            raise GoldReviewError("review_annotation_invalid") from exc
        records = {item.query_id: item for item in self._draft.records}
        record = records.get(query_id)
        if record is None:
            raise GoldReviewError("review_query_unknown")
        try:
            _validate_annotation_sources(annotation, record)
        except ValueError as exc:
            raise GoldReviewError("review_annotation_source_invalid") from exc
        with self._lock:
            decisions = list(self._state.decisions)
            index = next(
                (position for position, item in enumerate(decisions) if item.query_id == query_id),
                None,
            )
            if index is None:
                raise GoldReviewError("review_query_unknown")
            decisions[index] = GoldReviewDecision(
                query_id=query_id,
                status=cast(ReviewStatus, status_value),
                annotation=annotation,
            )
            state = self._state.model_copy(update={"decisions": tuple(decisions)})
            _atomic_state_write(self._state_path, _json_file_bytes(state))
            self._state = state


class _BlindController(_ReviewController):
    def __init__(self, packet: BlindPacketDataset, state_path: Path) -> None:
        self._packet = packet
        self._state_path = state_path
        self._state = _load_or_create_blind_state(state_path, packet)
        self._lock = threading.Lock()

    def public_payload(self) -> dict[str, Any]:
        with self._lock:
            decisions = {item.pair_id: item for item in self._state.decisions}
            return {
                "mode": "blind",
                "rubric": (
                    "좌우 답변의 출처나 시스템을 추측하지 말고 자연스러움과 사실 완결성을 각각 "
                    "독립적으로 선택하세요. 차이가 없으면 동점을 선택하세요."
                ),
                "items": [
                    {
                        "pair_id": pair.pair_id,
                        "query_id": pair.query_id,
                        "rater_key": pair.rater_key,
                        "question": pair.question,
                        "left_answer": pair.left_answer,
                        "right_answer": pair.right_answer,
                        "naturalness_preference": decisions[pair.pair_id].naturalness_preference,
                        "factual_completeness_preference": decisions[
                            pair.pair_id
                        ].factual_completeness_preference,
                    }
                    for pair in self._packet.pairs
                ],
            }

    def apply(self, payload: Mapping[str, Any]) -> None:
        required = {
            "pair_id",
            "naturalness_preference",
            "factual_completeness_preference",
        }
        if set(payload) != required:
            raise GoldReviewError("review_request_fields_invalid")
        pair_id = payload.get("pair_id")
        naturalness = payload.get("naturalness_preference")
        completeness = payload.get("factual_completeness_preference")
        allowed = {"left", "tie", "right"}
        if (
            not isinstance(pair_id, str)
            or naturalness not in allowed
            or completeness not in allowed
        ):
            raise GoldReviewError("review_request_invalid")
        pairs = {item.pair_id: item for item in self._packet.pairs}
        pair = pairs.get(pair_id)
        if pair is None:
            raise GoldReviewError("review_pair_unknown")
        if pair.left_answer_sha256 == pair.right_answer_sha256 and (
            naturalness != "tie" or completeness != "tie"
        ):
            raise GoldReviewError("blind_identical_answer_preference_invalid")
        with self._lock:
            decisions = list(self._state.decisions)
            index = next(
                (position for position, item in enumerate(decisions) if item.pair_id == pair_id),
                None,
            )
            if index is None:
                raise GoldReviewError("review_pair_unknown")
            decisions[index] = BlindReviewDecision(
                pair_id=pair.pair_id,
                query_id=pair.query_id,
                rater_key=pair.rater_key,
                naturalness_preference=cast(PairwisePreference, naturalness),
                factual_completeness_preference=cast(PairwisePreference, completeness),
            )
            state = self._state.model_copy(update={"decisions": tuple(decisions)})
            _atomic_state_write(self._state_path, _json_file_bytes(state))
            self._state = state


_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>CardRAG offline review</title><style>
body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#151515}
button,select,textarea{font:inherit}button{margin:.25rem;padding:.5rem 1rem}textarea{width:100%;min-height:24rem}
.answers{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.box{white-space:pre-wrap;border:1px solid #aaa;padding:1rem}
.muted{color:#555}.error{color:#a00}@media(max-width:750px){.answers{grid-template-columns:1fr}}
</style></head><body><h1>CardRAG offline review</h1><p id="rubric"></p><p id="progress"></p>
<main id="main"></main><p id="message" class="error"></p>
<script nonce="__NONCE__">"use strict";
const csrf="__CSRF__";let data=null,index=0;
const message=document.getElementById('message'),rubric=document.getElementById('rubric');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(method,body){const options={method,headers:{'X-CSRF-Token':csrf}};
if(body){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body)}
const response=await fetch('/api/state',options);if(!response.ok)throw new Error(await response.text());return response.json()}
function nav(delta){index=Math.max(0,Math.min(data.items.length-1,index+delta));render()}
function common(item){document.getElementById('progress').textContent=`${index+1} / ${data.items.length}`;
return `<button id="prev">이전</button><button id="next">다음</button><h2>${esc(item.query_id)}</h2>`}
function bindNav(){document.getElementById('prev').onclick=()=>nav(-1);document.getElementById('next').onclick=()=>nav(1)}
function renderGold(item){document.getElementById('main').innerHTML=common(item)+
`<p>상태: <strong>${esc(item.status)}</strong></p><h3>정확한 원문 후보</h3>`+
item.evidence.map(e=>`<section class="box"><b>${esc(e.product_name)} · p.${e.page}</b>\n${esc(e.text)}</section>`).join('')+
`<h3>Gold JSON</h3><textarea id="annotation"></textarea><div><button data-s="pending">보류</button>`+
`<button data-s="rejected">거절</button><button data-s="approved">승인</button></div>`;
document.getElementById('annotation').value=JSON.stringify(item.annotation,null,2);bindNav();
document.querySelectorAll('[data-s]').forEach(b=>b.onclick=async()=>{try{await api('POST',{query_id:item.query_id,status:b.dataset.s,
annotation:JSON.parse(document.getElementById('annotation').value)});data=await api('GET');render()}catch(e){message.textContent=e.message}})}
function choice(id,value){return `<select id="${id}"><option value="">선택</option><option value="left">왼쪽</option>`+
`<option value="tie">동점</option><option value="right">오른쪽</option></select>`}
function renderBlind(item){document.getElementById('main').innerHTML=common(item)+`<p>${esc(item.question)}</p>`+
`<div class="answers"><section><h3>왼쪽</h3><div class="box">${esc(item.left_answer)}</div></section>`+
`<section><h3>오른쪽</h3><div class="box">${esc(item.right_answer)}</div></section></div>`+
`<p>자연스러움 ${choice('natural',item.naturalness_preference||'')}</p>`+
`<p>사실 완결성 ${choice('complete',item.factual_completeness_preference||'')}</p><button id="save">저장</button>`;
document.getElementById('natural').value=item.naturalness_preference||'';
document.getElementById('complete').value=item.factual_completeness_preference||'';bindNav();
document.getElementById('save').onclick=async()=>{try{await api('POST',{pair_id:item.pair_id,
naturalness_preference:document.getElementById('natural').value,factual_completeness_preference:document.getElementById('complete').value});
data=await api('GET');if(index<data.items.length-1)index++;render()}catch(e){message.textContent=e.message}}}
function render(){message.textContent='';rubric.textContent=data.rubric;const item=data.items[index];
if(data.mode==='gold')renderGold(item);else renderBlind(item)}
api('GET').then(v=>{data=v;render()}).catch(e=>message.textContent=e.message);
</script></body></html>"""


def create_review_server(
    controller: _ReviewController,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create the secured loopback HTTP server; callers own its lifecycle."""

    if host != "127.0.0.1":
        raise GoldReviewError("review_host_must_be_loopback")
    if not 0 <= port <= 65_535:
        raise GoldReviewError("review_port_invalid")
    csrf_token = secrets.token_urlsafe(32)
    script_nonce = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        server_version = "CardRAGOfflineReview/1"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            # Avoid logging attacker-controlled paths, query strings, or headers.
            return

        def _expected_hosts(self) -> set[str]:
            address = cast(tuple[str, int], self.server.server_address)
            bound_port = address[1]
            return {f"127.0.0.1:{bound_port}", f"localhost:{bound_port}"}

        def _host_valid(self) -> bool:
            host_header = self.headers.get("Host", "")
            return host_header in self._expected_hosts()

        def _security_headers(self, *, content_type: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                f"script-src 'nonce-{script_nonce}'; style-src 'unsafe-inline'; "
                "connect-src 'self'; form-action 'none'",
            )

        def _send(self, status_code: int, body: bytes, *, content_type: str) -> None:
            self.send_response(status_code)
            self._security_headers(content_type=content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status_code: int, code: str) -> None:
            self._send(status_code, code.encode("ascii"), content_type="text/plain; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_valid():
                self._error(HTTPStatus.FORBIDDEN, "host_forbidden")
                return
            path = urlsplit(self.path)
            if path.query or path.fragment:
                self._error(HTTPStatus.NOT_FOUND, "not_found")
                return
            if path.path == "/":
                body = (
                    _HTML.replace("__CSRF__", csrf_token)
                    .replace("__NONCE__", script_nonce)
                    .encode("utf-8")
                )
                self._send(HTTPStatus.OK, body, content_type="text/html; charset=utf-8")
                return
            if path.path == "/api/state":
                body = canonical_json_bytes(controller.public_payload()) + b"\n"
                self._send(HTTPStatus.OK, body, content_type="application/json; charset=utf-8")
                return
            self._error(HTTPStatus.NOT_FOUND, "not_found")

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_valid():
                self._error(HTTPStatus.FORBIDDEN, "host_forbidden")
                return
            host_header = self.headers.get("Host", "")
            if self.headers.get("Origin", "") != f"http://{host_header}":
                self._error(HTTPStatus.FORBIDDEN, "origin_forbidden")
                return
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                self._error(HTTPStatus.FORBIDDEN, "csrf_invalid")
                return
            if self.headers.get_content_type() != "application/json":
                self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type_invalid")
                return
            path = urlsplit(self.path)
            if path.path != "/api/state" or path.query or path.fragment:
                self._error(HTTPStatus.NOT_FOUND, "not_found")
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "")
            except ValueError:
                self._error(HTTPStatus.LENGTH_REQUIRED, "content_length_invalid")
                return
            if not 0 < length <= MAX_REVIEW_BODY_BYTES:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_body_size_invalid")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._error(HTTPStatus.BAD_REQUEST, "request_body_incomplete")
                return
            try:
                value = json.loads(body.decode("utf-8"))
                if not isinstance(value, dict):
                    raise GoldReviewError("review_request_not_object")
                controller.apply(cast(dict[str, Any], value))
            except GoldReviewError as exc:
                self._error(HTTPStatus.BAD_REQUEST, exc.code)
                return
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                self._error(HTTPStatus.BAD_REQUEST, "review_request_json_invalid")
                return
            self._send(
                HTTPStatus.OK,
                b'{"ok":true}\n',
                content_type="application/json; charset=utf-8",
            )

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve_gold(draft_path: Path, state_path: Path, *, port: int) -> None:
    controller = _GoldController(_load_draft(draft_path), state_path)
    server = create_review_server(controller, port=port)
    try:
        print(f"gold review listening on http://127.0.0.1:{server.server_address[1]}")
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def serve_blind(packet_path: Path, state_path: Path, *, port: int) -> None:
    controller = _BlindController(_load_blind_packet(packet_path), state_path)
    server = create_review_server(controller, port=port)
    try:
        print(f"blind review listening on http://127.0.0.1:{server.server_address[1]}")
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline gold and blind review tooling")
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft = subparsers.add_parser("draft", help="build deterministic corpus-only gold drafts")
    draft.add_argument("--database", type=Path, required=True)
    draft.add_argument("--output", type=Path, required=True)
    draft.add_argument("--state", type=Path, required=True)
    draft.add_argument("--count", type=int, default=DEFAULT_QUERY_COUNT)
    draft.add_argument("--no-answer-count", type=int, default=DEFAULT_NO_ANSWER_COUNT)
    draft.add_argument("--seed", type=int, default=DEFAULT_SEED)

    gold_server = subparsers.add_parser("serve-gold", help="serve local gold review UI")
    gold_server.add_argument("--draft", type=Path, required=True)
    gold_server.add_argument("--state", type=Path, required=True)
    gold_server.add_argument("--port", type=int, default=8765)

    gold_seal = subparsers.add_parser("seal-gold", help="seal approved release gold JSONL")
    gold_seal.add_argument("--draft", type=Path, required=True)
    gold_seal.add_argument("--state", type=Path, required=True)
    gold_seal.add_argument("--output", type=Path, required=True)

    blind = subparsers.add_parser("prepare-blind", help="prepare anonymous balanced A/B packet")
    blind.add_argument("--gold", type=Path, required=True)
    blind.add_argument("--baseline-run", type=Path, required=True)
    blind.add_argument("--candidate-run", type=Path, required=True)
    blind.add_argument("--packet", type=Path, required=True)
    blind.add_argument("--state", type=Path, required=True)
    blind.add_argument("--rater-key", action="append", default=[])
    blind.add_argument("--seed", type=int, default=DEFAULT_SEED)

    blind_server = subparsers.add_parser("serve-blind", help="serve local anonymous A/B UI")
    blind_server.add_argument("--packet", type=Path, required=True)
    blind_server.add_argument("--state", type=Path, required=True)
    blind_server.add_argument("--port", type=int, default=8766)

    blind_seal = subparsers.add_parser("seal-blind", help="seal completed blind ratings")
    blind_seal.add_argument("--gold", type=Path, required=True)
    blind_seal.add_argument("--baseline-run", type=Path, required=True)
    blind_seal.add_argument("--candidate-run", type=Path, required=True)
    blind_seal.add_argument("--packet", type=Path, required=True)
    blind_seal.add_argument("--state", type=Path, required=True)
    blind_seal.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "draft":
            draft_dataset = write_draft(
                args.database,
                args.output,
                args.state,
                count=args.count,
                no_answer_count=args.no_answer_count,
                seed=args.seed,
            )
            print(draft_dataset.sha256)
        elif args.command == "serve-gold":
            serve_gold(args.draft, args.state, port=args.port)
        elif args.command == "seal-gold":
            print(seal_gold(args.draft, args.state, args.output))
        elif args.command == "prepare-blind":
            keys = tuple(args.rater_key) or ("anonymous-rater-01",)
            packet_dataset = prepare_blind(
                args.gold,
                args.baseline_run,
                args.candidate_run,
                args.packet,
                args.state,
                rater_keys=keys,
                seed=args.seed,
            )
            print(packet_dataset.sha256)
        elif args.command == "serve-blind":
            serve_blind(args.packet, args.state, port=args.port)
        elif args.command == "seal-blind":
            print(
                seal_blind(
                    args.gold,
                    args.baseline_run,
                    args.candidate_run,
                    args.packet,
                    args.state,
                    args.output,
                )
            )
        else:  # pragma: no cover - argparse makes this unreachable
            raise GoldReviewError("command_unknown")
    except GoldReviewError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
