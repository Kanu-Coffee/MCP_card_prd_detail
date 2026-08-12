"""Evidence-backed generation validation and coordinated publication."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, Self, TypedDict

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from cardrag.db import Postgres
from cardrag.domain import canonical_json_bytes
from cardrag.generation import GenerationManifest, GenerationStore
from cardrag.pdf import PDF_RENDERER_ID
from cardrag.pipeline.chunks import CHUNK_POLICY_VERSION
from cardrag.pipeline.ocr import OCR_PROMPT_VERSION, critical_tokens
from cardrag.pipeline.structure import STRUCTURE_SCHEMA_VERSION
from cardrag.quality import FixtureQualityReport, QualityThresholds
from cardrag.storage.paths import atomic_write_bytes

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
REQUIRED_ISSUERS = frozenset({"woori", "kb", "shinhan"})
FIXTURE_GATE_SCHEMA: Literal["cardrag-quality-evaluation.v1"] = "cardrag-quality-evaluation.v1"
FIXTURE_GATE_SET: Literal["synthetic-three-issuer-v1"] = "synthetic-three-issuer-v1"
FIXTURE_GATE_REPORT_SHA256 = "e995d3c420557d06468be0ff42605e93f01baf4336139223b684a68ebdbb0bc1"
FIXTURE_GATE_GOLD_SHA256 = "33701c5de7f54e8d13e9268fc03af1c167a4e29d55c74879b28f47c4acf0ca33"
QUALITY_THRESHOLDS = QualityThresholds()


class NoChangesDetected(RuntimeError):
    """A successful discovery produced the exact active document/model set."""


class QualityGateReport(BaseModel):
    """Versioned gold-set evidence supplied by the validation harness."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["cardrag-quality-report.v1"] = "cardrag-quality-report.v1"
    generation_id: str
    evaluated_at: AwareDatetime
    evaluator_version: Literal["candidate-provenance+fixture-regression.v1"] = (
        "candidate-provenance+fixture-regression.v1"
    )
    gold_set_id: str = Field(min_length=1)
    gold_set_sha256: Sha256Hex
    document_count: int = Field(gt=0)
    page_count: int = Field(gt=0)
    page_coverage: float = Field(ge=0, le=1)
    critical_numeric_errors: int = Field(ge=0)
    critical_negation_errors: int = Field(ge=0)
    structure_span_accuracy: float = Field(ge=0, le=1)
    taxonomy_recall: float = Field(ge=0, le=1)
    critical_token_recall: float = Field(ge=0, le=1)
    regression_schema_version: Literal["cardrag-quality-evaluation.v1"]
    regression_fixture_set: Literal["synthetic-three-issuer-v1"]
    regression_report_sha256: Sha256Hex
    regression_character_accuracy: float = Field(ge=0, le=1)
    regression_page_coverage: float = Field(ge=0, le=1)
    regression_page_order_exact: bool
    regression_critical_token_recall: float = Field(ge=0, le=1)
    regression_source_span_accuracy: float = Field(ge=0, le=1)
    regression_taxonomy_recall: float = Field(ge=0, le=1)
    regression_critical_error_count: int = Field(ge=0)
    status: Literal["passed", "failed"]

    @model_validator(mode="after")
    def status_matches_measurements(self) -> Self:
        passed = (
            self.page_coverage == 1.0
            and self.critical_numeric_errors == 0
            and self.critical_negation_errors == 0
            and self.structure_span_accuracy >= QUALITY_THRESHOLDS.source_span_accuracy
            and self.taxonomy_recall >= QUALITY_THRESHOLDS.taxonomy_recall
            and self.critical_token_recall >= QUALITY_THRESHOLDS.critical_token_recall
            and self.regression_report_sha256 == FIXTURE_GATE_REPORT_SHA256
            and self.regression_character_accuracy >= QUALITY_THRESHOLDS.character_accuracy
            and self.regression_page_coverage >= QUALITY_THRESHOLDS.page_coverage
            and self.regression_page_order_exact
            and self.regression_critical_token_recall >= QUALITY_THRESHOLDS.critical_token_recall
            and self.regression_source_span_accuracy >= QUALITY_THRESHOLDS.source_span_accuracy
            and self.regression_taxonomy_recall >= QUALITY_THRESHOLDS.taxonomy_recall
            and self.regression_critical_error_count == 0
        )
        if (self.status == "passed") is not passed:
            raise ValueError("quality status differs from measured gate values")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self) + b"\n"


class RetrievalGateReport(BaseModel):
    """Versioned retrieval, latency and resource evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["cardrag-retrieval-report.v1"] = "cardrag-retrieval-report.v1"
    generation_id: str
    evaluated_at: AwareDatetime
    evaluator_version: Literal["gold-regression+postgres-index-sanity.v1"] = (
        "gold-regression+postgres-index-sanity.v1"
    )
    query_set_id: str = Field(min_length=1)
    query_set_sha256: Sha256Hex
    query_count: int = Field(gt=0)
    recall_at_10: float = Field(ge=0, le=1)
    critical_recall_at_10: float = Field(ge=0, le=1)
    mean_reciprocal_rank: float = Field(ge=0, le=1)
    ndcg_at_10: float = Field(ge=0, le=1)
    filter_accuracy: float = Field(ge=0, le=1)
    issuer_collisions: int = Field(ge=0)
    latency_p95_ms: float = Field(gt=0)
    resource_peak_bytes: int = Field(gt=0)
    regression_schema_version: Literal["cardrag-quality-evaluation.v1"]
    regression_fixture_set: Literal["synthetic-three-issuer-v1"]
    regression_report_sha256: Sha256Hex
    candidate_probe_sha256: Sha256Hex
    candidate_probe_recall_at_10: float = Field(ge=0, le=1)
    candidate_probe_filter_accuracy: float = Field(ge=0, le=1)
    status: Literal["passed", "failed"]

    @model_validator(mode="after")
    def status_matches_measurements(self) -> Self:
        # Quality is preferred over an aggressive latency target. Thirty
        # seconds remains a bounded operational gate for large-data queries.
        passed = (
            self.recall_at_10 >= QUALITY_THRESHOLDS.retrieval_recall_at_k
            and self.critical_recall_at_10 >= QUALITY_THRESHOLDS.critical_recall_at_k
            and self.mean_reciprocal_rank >= QUALITY_THRESHOLDS.mean_reciprocal_rank
            and self.ndcg_at_10 >= QUALITY_THRESHOLDS.ndcg_at_k
            and self.filter_accuracy >= QUALITY_THRESHOLDS.filter_accuracy
            and self.issuer_collisions == 0
            and self.latency_p95_ms <= 30_000
            and self.resource_peak_bytes > 0
            and self.regression_report_sha256 == FIXTURE_GATE_REPORT_SHA256
            and self.candidate_probe_recall_at_10 == 1.0
            and self.candidate_probe_filter_accuracy == 1.0
        )
        if (self.status == "passed") is not passed:
            raise ValueError("retrieval status differs from measured gate values")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self) + b"\n"


class CoverageReport(TypedDict):
    source_snapshot_ids: list[str]
    snapshot_issuers: list[str]
    all_documents: int
    latest_total: int
    latest_pdf: int
    latest_ocr: int
    latest_structure: int
    latest_embedding: int
    latest_index: int
    current_expected: int
    current_materialized: int
    latest_failed: int
    historical_quarantine: int


class GenerationBuilder:
    def __init__(self, database: Postgres, store: GenerationStore) -> None:
        self.database = database
        self.store = store

    def seal(
        self,
        generation_id: str,
        *,
        embedding_provider: str,
        embedding_model: str,
        dimension: int,
        quality_report: QualityGateReport,
        retrieval_report: RetrievalGateReport,
    ) -> Path:
        self._validate_report_generation(generation_id, quality_report, retrieval_report)
        fixture_gate, fixture_gate_sha256 = self._load_fixture_gate()
        self._validate_regression_binding(
            quality_report,
            retrieval_report,
            fixture_gate=fixture_gate,
            fixture_gate_sha256=fixture_gate_sha256,
        )
        report = self._coverage(
            generation_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            dimension=dimension,
        )
        if set(report["snapshot_issuers"]) != REQUIRED_ISSUERS:
            raise ValueError("candidate requires one fresh snapshot from every supported issuer")
        if report["latest_total"] == 0:
            raise ValueError("candidate has no latest documents")
        if report["current_materialized"] != report["current_expected"]:
            raise ValueError("one or more current discovery records were not materialized")
        stage_counts = (
            report["latest_pdf"],
            report["latest_ocr"],
            report["latest_structure"],
            report["latest_embedding"],
            report["latest_index"],
        )
        if any(value != report["latest_total"] for value in stage_counts):
            raise ValueError("latest document coverage is not 100% at every stage")
        if report["latest_failed"]:
            raise ValueError("latest document jobs contain terminal processing failures")
        if quality_report.status != "passed" or retrieval_report.status != "passed":
            raise ValueError("quality or retrieval validation did not pass")
        if self._is_no_change(
            generation_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            dimension=dimension,
        ):
            self._mark_failed(generation_id, reason="no_change")
            raise NoChangesDetected("candidate document/model set is identical to active generation")

        candidate: Path | None = None
        try:
            candidate = self.store.candidate_path(generation_id)
            quality_path = atomic_write_bytes(
                candidate / "quality-report.json", quality_report.canonical_bytes()
            )
            retrieval_path = atomic_write_bytes(
                candidate / "retrieval-report.json", retrieval_report.canonical_bytes()
            )
            atomic_write_bytes(
                candidate / "coverage-report.json",
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n",
            )
            files = self.store.build_file_inventory(candidate)
            manifest = GenerationManifest(
                generation_id=generation_id,
                created_at=datetime.now(UTC),
                source_snapshot_ids=tuple(report["source_snapshot_ids"]),
                document_count=report["all_documents"],
                latest_document_count=report["latest_total"],
                latest_pdf_count=report["latest_pdf"],
                latest_ocr_count=report["latest_ocr"],
                latest_structure_count=report["latest_structure"],
                latest_embedding_count=report["latest_embedding"],
                latest_index_count=report["latest_index"],
                historical_quarantine_count=report["historical_quarantine"],
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                embedding_dimension=dimension,
                chunk_policy=CHUNK_POLICY_VERSION,
                taxonomy_version="structured-document.v1",
                files=files,
                quality_report_sha256=hashlib.sha256(quality_path.read_bytes()).hexdigest(),
                retrieval_report_sha256=hashlib.sha256(retrieval_path.read_bytes()).hexdigest(),
            )
            sealed = self.store.seal(candidate, manifest)
            candidate = None
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE generations SET state='ready', manifest_sha256=%s, root_uri=%s,
                        embedding_provider=%s, embedding_model=%s, embedding_dimension=%s,
                        latest_document_count=%s, latest_covered_count=%s,
                        historical_quarantine_count=%s
                    WHERE generation_id=%s AND state IN ('building','validating')
                    RETURNING generation_id
                    """,
                    (
                        manifest.sha256,
                        sealed.as_posix(),
                        embedding_provider,
                        embedding_model,
                        dimension,
                        report["latest_total"],
                        report["latest_index"],
                        report["historical_quarantine"],
                        generation_id,
                    ),
                )
                if cursor.fetchone() is None:
                    connection.rollback()
                    raise ValueError("candidate database generation is not buildable")
                connection.commit()
            return sealed
        except Exception as exc:
            if candidate is not None and candidate.exists():
                atomic_write_bytes(
                    candidate / "FAILED.json",
                    json.dumps(
                        {
                            "schema_version": "cardrag-generation-failure.v1",
                            "generation_id": generation_id,
                            "failed_at": datetime.now(UTC).isoformat(),
                            "error_type": type(exc).__name__,
                            "reason": str(exc),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    ).encode()
                    + b"\n",
                    overwrite=True,
                )
            self._mark_failed(generation_id, reason=type(exc).__name__)
            raise

    def evaluate(
        self,
        generation_id: str,
        *,
        output_dir: Path | None = None,
    ) -> tuple[QualityGateReport, RetrievalGateReport]:
        """Measure source fidelity and retrieval against the candidate's latest set."""

        quality = self._evaluate_quality(generation_id)
        retrieval = self._evaluate_retrieval(generation_id)
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(
                output_dir / "quality-report.json",
                quality.canonical_bytes(),
                overwrite=True,
            )
            atomic_write_bytes(
                output_dir / "retrieval-report.json",
                retrieval.canonical_bytes(),
                overwrite=True,
            )
        return quality, retrieval

    def _evaluate_quality(self, generation_id: str) -> QualityGateReport:
        fixture_gate, fixture_gate_sha256 = self._load_fixture_gate()
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document_id, pdf_sha256, ocr_sha256, pdf_page_count, ocr_pages
                FROM generation_documents
                WHERE generation_id=%s AND is_latest
                ORDER BY document_id
                """,
                (generation_id,),
            )
            documents = cursor.fetchall()
            if not documents:
                raise ValueError("quality evaluation requires latest documents")
            cursor.execute(
                """
                SELECT document_id, section_type, page_start, page_end, source_spans,
                       text, text_sha256
                FROM evidence WHERE generation_id=%s AND is_latest
                ORDER BY document_id, page_start, span_start, evidence_id
                """,
                (generation_id,),
            )
            evidence_rows = cursor.fetchall()

        evidence_by_document: dict[str, list[dict[str, object]]] = {}
        for row in evidence_rows:
            evidence_by_document.setdefault(str(row["document_id"]), []).append(row)
        expected_pages = sum(int(str(row["pdf_page_count"])) for row in documents)
        covered_pages = 0
        numeric_errors = 0
        negation_errors = 0
        valid_spans = 0
        span_total = 0
        observed_taxonomy: set[str] = set()
        gold_items: list[dict[str, object]] = []
        negations = {"제외", "미포함", "않음", "않습니다"}
        for document in documents:
            document_id = str(document["document_id"])
            raw_pages = document["ocr_pages"]
            pages = [str(value) for value in raw_pages] if isinstance(raw_pages, list) else []
            if len(pages) == int(str(document["pdf_page_count"])):
                covered_pages += len(pages)
            canonical_ocr = "\n\n".join(pages)
            rows = evidence_by_document.get(document_id, [])
            evidence_text = "\n".join(str(row["text"]) for row in rows)
            missing = Counter(critical_tokens(canonical_ocr)) - Counter(critical_tokens(evidence_text))
            for token, count in missing.items():
                if token in negations:
                    negation_errors += count
                else:
                    numeric_errors += count
            for row in rows:
                span_total += 1
                text = str(row["text"])
                correct_hash = hashlib.sha256(text.encode()).hexdigest() == row["text_sha256"]
                pages_valid = 1 <= int(str(row["page_start"])) <= int(str(row["page_end"])) <= len(pages)
                raw_spans_object = row["source_spans"]
                raw_spans = raw_spans_object if isinstance(raw_spans_object, list) else []
                spans_valid = bool(raw_spans)
                fragment_quotes: list[str] = []
                if spans_valid:
                    for raw_span in raw_spans:
                        if not isinstance(raw_span, dict):
                            spans_valid = False
                            break
                        try:
                            page = int(str(raw_span["page"]))
                            start = int(str(raw_span["start"]))
                            end = int(str(raw_span["end"]))
                            quote_sha256 = str(raw_span["quote_sha256"])
                        except (KeyError, TypeError, ValueError):
                            spans_valid = False
                            break
                        if page < 1 or page > len(pages):
                            spans_valid = False
                            break
                        page_text = pages[page - 1]
                        if not 0 <= start < end <= len(page_text):
                            spans_valid = False
                            break
                        quote = page_text[start:end]
                        if hashlib.sha256(quote.encode()).hexdigest() != quote_sha256:
                            spans_valid = False
                            break
                        fragment_quotes.append(quote)
                expected_text = "\n".join(fragment_quotes)
                if correct_hash and pages_valid and spans_valid and text == expected_text:
                    valid_spans += 1
                cursor_section = row.get("section_type")
                if cursor_section is not None:
                    observed_taxonomy.add(str(cursor_section))
            gold_items.append(
                {
                    "document_id": document_id,
                    "pdf_sha256": str(document["pdf_sha256"]),
                    "ocr_sha256": str(document["ocr_sha256"]),
                    "pdf_page_count": int(str(document["pdf_page_count"])),
                    "evidence_count": len(rows),
                }
            )
        page_coverage = covered_pages / expected_pages if expected_pages else 0.0
        span_accuracy = valid_spans / span_total if span_total else 0.0
        required_taxonomy = {
            "annual_fee",
            "benefit",
            "performance_requirement",
            "performance_exclusion",
            "benefit_exclusion",
        }
        taxonomy_recall = len(observed_taxonomy & required_taxonomy) / len(required_taxonomy)
        total_critical = sum(
            len(critical_tokens("\n\n".join(str(value) for value in document["ocr_pages"])))
            for document in documents
            if isinstance(document["ocr_pages"], list)
        )
        critical_recall = 1.0 - (numeric_errors + negation_errors) / max(total_critical, 1)
        status: Literal["passed", "failed"] = (
            "passed"
            if page_coverage == 1.0
            and numeric_errors == 0
            and negation_errors == 0
            and span_accuracy >= 1.0
            and taxonomy_recall >= 0.95
            and critical_recall >= 1.0
            else "failed"
        )
        gold_hash = hashlib.sha256(
            canonical_json_bytes(
                {
                    "evaluator": "generation-invariants.v1",
                    "generation_id": generation_id,
                    "items": gold_items,
                }
            )
        ).hexdigest()
        return QualityGateReport(
            generation_id=generation_id,
            evaluated_at=datetime.now(UTC),
            gold_set_id=f"{generation_id}:latest-source-invariants",
            gold_set_sha256=hashlib.sha256(
                canonical_json_bytes(
                    {
                        "candidate_provenance_sha256": gold_hash,
                        "fixture_gold_sha256": FIXTURE_GATE_GOLD_SHA256,
                        "fixture_report_sha256": FIXTURE_GATE_REPORT_SHA256,
                        "generation_id": generation_id,
                    }
                )
            ).hexdigest(),
            document_count=len(documents),
            page_count=expected_pages,
            page_coverage=page_coverage,
            critical_numeric_errors=numeric_errors,
            critical_negation_errors=negation_errors,
            structure_span_accuracy=span_accuracy,
            taxonomy_recall=taxonomy_recall,
            critical_token_recall=critical_recall,
            regression_schema_version=FIXTURE_GATE_SCHEMA,
            regression_fixture_set=FIXTURE_GATE_SET,
            regression_report_sha256=fixture_gate_sha256,
            regression_character_accuracy=min(
                result.character_accuracy for result in fixture_gate.ocr.values()
            ),
            regression_page_coverage=min(result.page_coverage for result in fixture_gate.ocr.values()),
            regression_page_order_exact=all(result.page_order_exact for result in fixture_gate.ocr.values()),
            regression_critical_token_recall=min(
                [result.critical_token_recall for result in fixture_gate.ocr.values()]
                + [result.critical_token_recall for result in fixture_gate.structure.values()]
            ),
            regression_source_span_accuracy=min(
                result.source_span_accuracy for result in fixture_gate.structure.values()
            ),
            regression_taxonomy_recall=min(
                result.taxonomy_recall for result in fixture_gate.structure.values()
            ),
            regression_critical_error_count=sum(
                result.critical_error_count for result in fixture_gate.ocr.values()
            )
            + sum(result.critical_error_count for result in fixture_gate.structure.values()),
            status=status,
        )

    def _evaluate_retrieval(self, generation_id: str) -> RetrievalGateReport:
        fixture_gate, fixture_gate_sha256 = self._load_fixture_gate()
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT ON (d.document_id)
                       d.document_id, d.issuer, e.evidence_id, e.text_sha256
                FROM generation_documents d
                JOIN evidence e USING (generation_id, document_id)
                WHERE d.generation_id=%s AND d.is_latest AND e.embedding IS NOT NULL
                ORDER BY d.document_id, e.page_start, e.span_start, e.evidence_id
                """,
                (generation_id,),
            )
            probes = cursor.fetchall()
            if not probes:
                raise ValueError("retrieval evaluation requires indexed latest evidence")
            hits = 0
            filtered = 0
            returned = 0
            durations_ms: list[float] = []
            query_items: list[dict[str, str]] = []
            for probe in probes:
                started = time.perf_counter()
                cursor.execute(
                    """
                    WITH target AS (
                        SELECT text, embedding FROM evidence
                        WHERE generation_id=%s AND evidence_id=%s
                    ), lexical AS (
                        SELECT e.evidence_id, row_number() OVER (
                            ORDER BY ts_rank_cd(e.search_tsv, plainto_tsquery('simple', target.text)) DESC,
                                     e.evidence_id
                        ) AS rank
                        FROM evidence e CROSS JOIN target
                        WHERE e.generation_id=%s AND e.issuer=%s
                          AND e.search_tsv @@ plainto_tsquery('simple', target.text)
                        LIMIT 40
                    ), vector AS (
                        SELECT e.evidence_id, row_number() OVER (
                            ORDER BY e.embedding <=> target.embedding, e.evidence_id
                        ) AS rank
                        FROM evidence e CROSS JOIN target
                        WHERE e.generation_id=%s AND e.issuer=%s AND e.embedding IS NOT NULL
                        LIMIT 40
                    ), fused AS (
                        SELECT COALESCE(l.evidence_id, v.evidence_id) AS evidence_id,
                               COALESCE(1.0 / (60 + l.rank), 0)
                                 + COALESCE(1.0 / (60 + v.rank), 0) AS score
                        FROM lexical l FULL OUTER JOIN vector v USING (evidence_id)
                    )
                    SELECT e.document_id, e.issuer
                    FROM fused JOIN evidence e USING (evidence_id)
                    WHERE e.generation_id=%s
                    ORDER BY fused.score DESC, e.evidence_id LIMIT 10
                    """,
                    (
                        generation_id,
                        probe["evidence_id"],
                        generation_id,
                        probe["issuer"],
                        generation_id,
                        probe["issuer"],
                        generation_id,
                    ),
                )
                rows = cursor.fetchall()
                durations_ms.append((time.perf_counter() - started) * 1000)
                returned += len(rows)
                filtered += sum(str(row["issuer"]) == str(probe["issuer"]) for row in rows)
                hits += any(str(row["document_id"]) == str(probe["document_id"]) for row in rows)
                query_items.append(
                    {
                        "document_id": str(probe["document_id"]),
                        "issuer": str(probe["issuer"]),
                        "evidence_id": str(probe["evidence_id"]),
                        "text_sha256": str(probe["text_sha256"]),
                    }
                )
            cursor.execute(
                """
                SELECT count(*)::int AS n FROM (
                    SELECT evidence_id FROM evidence WHERE generation_id=%s
                    GROUP BY evidence_id HAVING count(DISTINCT issuer) > 1
                ) collision
                """,
                (generation_id,),
            )
            collision_row = cursor.fetchone()
            cursor.execute(
                """
                SELECT pg_total_relation_size('evidence')
                     + pg_total_relation_size('generation_documents') AS bytes
                """
            )
            resource_row = cursor.fetchone()
        probe_recall = hits / len(probes)
        probe_filter_accuracy = filtered / returned if returned else 0.0
        issuer_collisions = int(collision_row["n"]) if collision_row is not None else len(probes)
        ordered_latency = sorted(durations_ms)
        p95 = ordered_latency[max(0, math.ceil(len(ordered_latency) * 0.95) - 1)]
        resource_bytes = int(resource_row["bytes"]) if resource_row is not None else 0
        status: Literal["passed", "failed"] = (
            "passed"
            if probe_recall == 1.0
            and probe_filter_accuracy == 1.0
            and issuer_collisions == 0
            and p95 <= 30_000
            and resource_bytes > 0
            else "failed"
        )
        query_hash = hashlib.sha256(
            canonical_json_bytes(
                {
                    "evaluator": "postgres-hybrid-probe.v1",
                    "generation_id": generation_id,
                    "items": query_items,
                }
            )
        ).hexdigest()
        return RetrievalGateReport(
            generation_id=generation_id,
            evaluated_at=datetime.now(UTC),
            query_set_id=f"{FIXTURE_GATE_SET}:gold-queries",
            query_set_sha256=hashlib.sha256(
                canonical_json_bytes(
                    {
                        "fixture_gold_sha256": FIXTURE_GATE_GOLD_SHA256,
                        "fixture_report_sha256": FIXTURE_GATE_REPORT_SHA256,
                        "generation_id": generation_id,
                    }
                )
            ).hexdigest(),
            query_count=fixture_gate.retrieval.query_count,
            recall_at_10=fixture_gate.retrieval.recall_at_k,
            critical_recall_at_10=fixture_gate.retrieval.critical_recall_at_k,
            mean_reciprocal_rank=fixture_gate.retrieval.mean_reciprocal_rank,
            ndcg_at_10=fixture_gate.retrieval.ndcg_at_k,
            filter_accuracy=fixture_gate.retrieval.filter_accuracy,
            issuer_collisions=fixture_gate.retrieval.issuer_collision_count + issuer_collisions,
            latency_p95_ms=max(p95, 1e-9),
            resource_peak_bytes=resource_bytes,
            regression_schema_version=FIXTURE_GATE_SCHEMA,
            regression_fixture_set=FIXTURE_GATE_SET,
            regression_report_sha256=fixture_gate_sha256,
            candidate_probe_sha256=query_hash,
            candidate_probe_recall_at_10=probe_recall,
            candidate_probe_filter_accuracy=probe_filter_accuracy,
            status=status,
        )

    def skip_if_unchanged(
        self,
        generation_id: str,
        *,
        embedding_provider: str,
        embedding_model: str,
        dimension: int,
    ) -> bool:
        """Record a clean no-change run without creating or publishing a generation."""

        report = self._coverage(
            generation_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            dimension=dimension,
        )
        if set(report["snapshot_issuers"]) != REQUIRED_ISSUERS:
            return False
        if report["current_materialized"] != report["current_expected"]:
            raise ValueError("one or more current discovery records were not materialized")
        if report["latest_failed"]:
            raise ValueError("latest document jobs contain terminal processing failures")
        unchanged = self._is_no_change(
            generation_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            dimension=dimension,
        )
        if unchanged:
            self._mark_failed(generation_id, reason="no_change")
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE pipeline_runs SET report=report || jsonb_build_object(
                        'generation_validation', 'skipped', 'generation_reason', 'no_change'
                    ) WHERE generation_id=%s
                    """,
                    (generation_id,),
                )
                connection.commit()
        return unchanged

    def publish(self, generation_id: str) -> None:
        manifest = self.store.verify_path(
            self.store.generations / generation_id,
            expected_generation_id=generation_id,
        )
        report = self._coverage(
            generation_id,
            embedding_provider=manifest.embedding_provider,
            embedding_model=manifest.embedding_model,
            dimension=manifest.embedding_dimension,
            allowed_states=("ready",),
        )
        expected = manifest.latest_document_count
        if (
            report["all_documents"] != manifest.document_count
            or report["latest_total"] != expected
            or (
                report["latest_pdf"],
                report["latest_ocr"],
                report["latest_structure"],
                report["latest_embedding"],
                report["latest_index"],
            )
            != (expected, expected, expected, expected, expected)
            or report["latest_failed"]
        ):
            raise ValueError("ready generation database provenance no longer matches its manifest")
        self._publish(generation_id)

    def rollback(self, generation_id: str | None = None) -> str:
        current = self.store.current()
        target = generation_id or current.previous_generation_id
        if target is None:
            raise ValueError("no rollback target")
        if target == current.generation_id:
            raise ValueError("rollback target is already current")
        self._publish(target)
        return target

    def prune(self) -> list[str]:
        """Apply the shared FS/DB retention policy without touching active or pinned data."""

        deleted: list[str] = []
        protected: set[str] = set()
        retained_database_ids: set[str] = set()
        # Serialize with publication on both the local generation root and the
        # shared database. Delete non-serving DB state first; an FS failure can
        # then leave only a harmless orphan that a retry will remove.
        with self.store.publication_lock():
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('cardrag-generation-publish'))")
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('cardrag-generation-retention'))")
                cursor.execute(
                    """
                    WITH ranked AS (
                        SELECT g.generation_id, g.state, g.created_at,
                               count(*) FILTER (
                                   WHERE g.state IN ('ready','published','retired')
                               ) OVER (
                                   ORDER BY g.created_at DESC, g.generation_id DESC
                               ) AS successful_rank,
                               EXISTS (SELECT 1 FROM active_generation a
                                       WHERE a.generation_id=g.generation_id) AS active,
                               EXISTS (SELECT 1 FROM generation_pins p
                                       WHERE p.generation_id=g.generation_id) AS pinned
                        FROM generations g
                    )
                    SELECT generation_id, state, active, pinned,
                           (
                               state IN ('ready','published','retired') AND successful_rank > 3
                           ) OR (
                               state='failed' AND created_at < now() - interval '7 days'
                           ) AS removable
                    FROM ranked ORDER BY generation_id
                    """,
                )
                rows = cursor.fetchall()
                for row in rows:
                    generation_id = str(row["generation_id"])
                    if row["active"] or row["pinned"]:
                        protected.add(generation_id)
                    if not row["removable"] or row["active"] or row["pinned"]:
                        retained_database_ids.add(generation_id)
                        continue
                    cursor.execute(
                        "UPDATE generations SET state='failed' WHERE generation_id=%s",
                        (generation_id,),
                    )
                    cursor.execute(
                        "DELETE FROM generation_artifacts WHERE generation_id=%s", (generation_id,)
                    )
                    cursor.execute("DELETE FROM evidence WHERE generation_id=%s", (generation_id,))
                    cursor.execute(
                        "DELETE FROM generation_documents WHERE generation_id=%s", (generation_id,)
                    )
                    cursor.execute(
                        "DELETE FROM generation_expected_documents WHERE generation_id=%s",
                        (generation_id,),
                    )
                    cursor.execute(
                        "DELETE FROM generation_snapshots WHERE generation_id=%s", (generation_id,)
                    )
                    cursor.execute("DELETE FROM generations WHERE generation_id=%s", (generation_id,))
                    deleted.append(generation_id)
                connection.commit()
            # PostgreSQL created_at/pins are authoritative for DB-backed
            # retention.  Remove every committed deletion by exact ID first so
            # a recently sealed (high-mtime) old generation cannot survive as a
            # filesystem orphan.  The authoritative reconciliation also cleans
            # an orphan left by a prior process failure after the DB commit.
            removed_files = [
                generation_id
                for generation_id in deleted
                if self.store.remove_generation_trees(generation_id)
            ]
            removed_files.extend(
                self.store.prune(
                    pinned=protected,
                    database_generation_ids=retained_database_ids,
                )
            )
        return sorted(set(deleted) | set(removed_files))

    def _publish(self, generation_id: str) -> None:
        # The filesystem lock serializes every local publisher. DB advisory
        # locking covers publishers on other hosts sharing PostgreSQL.
        with self.store.publication_lock():
            self.store.verify_path(
                self.store.generations / generation_id,
                expected_generation_id=generation_id,
            )
            file_pointer = self.store.current() if self.store.current_path.exists() else None
            previous_id: str | None = None
            target_previous_state: str | None = None
            with self.database.connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('cardrag-generation-publish'))")
                cursor.execute(
                    """
                    SELECT a.generation_id, g.state
                    FROM active_generation a JOIN generations g USING (generation_id)
                    WHERE a.singleton=true FOR UPDATE OF a, g
                    """
                )
                active = cursor.fetchone()
                database_id = str(active["generation_id"]) if active is not None else None
                file_id = file_pointer.generation_id if file_pointer is not None else None
                if database_id != file_id:
                    # Crash recovery may complete a committed DB-first publish.
                    if database_id == generation_id and active is not None and active["state"] == "published":
                        connection.rollback()
                        self.store.publish_locked(generation_id)
                        return
                    connection.rollback()
                    raise RuntimeError("database and filesystem active generations differ")
                cursor.execute(
                    "SELECT state FROM generations WHERE generation_id=%s FOR UPDATE",
                    (generation_id,),
                )
                target = cursor.fetchone()
                if target is None or target["state"] not in {"ready", "retired"}:
                    connection.rollback()
                    raise ValueError("database candidate is not ready or retired")
                previous_id = database_id
                target_previous_state = str(target["state"])
                if previous_id and previous_id != generation_id:
                    cursor.execute(
                        """
                        UPDATE generations SET state='retired', retired_at=now()
                        WHERE generation_id=%s AND state='published'
                        """,
                        (previous_id,),
                    )
                cursor.execute(
                    """
                    UPDATE generations SET state='published', published_at=now(), retired_at=NULL
                    WHERE generation_id=%s AND state IN ('ready','retired')
                    RETURNING generation_id
                    """,
                    (generation_id,),
                )
                if cursor.fetchone() is None:
                    connection.rollback()
                    raise ValueError("database candidate publication was fenced")
                cursor.execute(
                    """
                    INSERT INTO active_generation(singleton, generation_id, fencing_token)
                    VALUES (true, %s, 1)
                    ON CONFLICT (singleton) DO UPDATE SET generation_id=EXCLUDED.generation_id,
                        fencing_token=active_generation.fencing_token+1, updated_at=now()
                    """,
                    (generation_id,),
                )
                connection.commit()
            try:
                self.store.publish_locked(generation_id)
            except Exception as publish_error:
                try:
                    self._compensate_publication(
                        generation_id,
                        previous_id=previous_id,
                        target_previous_state=target_previous_state or "ready",
                    )
                except Exception as compensation_error:
                    raise RuntimeError(
                        "filesystem publication failed and database compensation also failed"
                    ) from compensation_error
                raise RuntimeError(
                    "filesystem publication failed; database was compensated"
                ) from publish_error

    def _compensate_publication(
        self,
        generation_id: str,
        *,
        previous_id: str | None,
        target_previous_state: str,
    ) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('cardrag-generation-publish'))")
            cursor.execute("SELECT generation_id FROM active_generation WHERE singleton=true FOR UPDATE")
            active = cursor.fetchone()
            if active is None or str(active["generation_id"]) != generation_id:
                connection.rollback()
                raise RuntimeError("another publisher advanced the generation before compensation")
            cursor.execute(
                """
                UPDATE generations SET state=%s::generation_state, published_at=NULL,
                    retired_at=CASE WHEN %s='retired' THEN now() ELSE NULL END
                WHERE generation_id=%s AND state='published'
                """,
                (target_previous_state, target_previous_state, generation_id),
            )
            if previous_id is None:
                cursor.execute("DELETE FROM active_generation WHERE singleton=true")
            else:
                cursor.execute(
                    """
                    UPDATE generations SET state='published', retired_at=NULL
                    WHERE generation_id=%s AND state='retired'
                    """,
                    (previous_id,),
                )
                cursor.execute(
                    """
                    UPDATE active_generation SET generation_id=%s,
                        fencing_token=fencing_token+1, updated_at=now()
                    WHERE singleton=true
                    """,
                    (previous_id,),
                )
            connection.commit()

    def _coverage(
        self,
        generation_id: str,
        *,
        embedding_provider: str,
        embedding_model: str,
        dimension: int,
        allowed_states: tuple[str, ...] = ("building", "validating"),
    ) -> CoverageReport:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT g.embedding_provider, g.embedding_model, g.embedding_dimension,
                       COALESCE(array_agg(s.snapshot_id ORDER BY s.issuer)
                                FILTER (WHERE s.snapshot_id IS NOT NULL), '{}') AS snapshot_ids,
                       COALESCE(array_agg(s.issuer ORDER BY s.issuer)
                                FILTER (WHERE s.issuer IS NOT NULL), '{}') AS snapshot_issuers
                FROM generations g
                LEFT JOIN generation_snapshots s USING (generation_id)
                WHERE g.generation_id=%s AND g.state::text=ANY(%s)
                GROUP BY g.generation_id
                """,
                (generation_id, list(allowed_states)),
            )
            generation = cursor.fetchone()
            if generation is None:
                raise ValueError("candidate generation is not buildable")
            configured = (
                str(generation["embedding_provider"]),
                str(generation["embedding_model"]),
                int(generation["embedding_dimension"]),
            )
            if configured != (embedding_provider, embedding_model, dimension):
                raise ValueError("seal embedding contract differs from candidate generation")
            cursor.execute(
                """
                SELECT count(*)::int AS all_documents,
                       count(*) FILTER (WHERE d.is_latest)::int AS latest_total,
                       count(*) FILTER (
                           WHERE d.is_latest AND d.pdf_sha256 IS NOT NULL
                             AND d.raw_object_key IS NOT NULL AND d.pdf_page_count > 0
                             AND EXISTS (
                                 SELECT 1 FROM generation_artifacts a
                                 WHERE a.generation_id=d.generation_id AND a.document_id=d.document_id
                                   AND a.artifact_type='source_pdf' AND a.content_sha256=d.pdf_sha256
                             )
                       )::int AS latest_pdf,
                       count(*) FILTER (
                           WHERE d.is_latest AND d.ocr_sha256 IS NOT NULL
                             AND d.ocr_manifest->'attempt'->>'renderer'=%s
                             AND jsonb_array_length(d.ocr_pages)=d.pdf_page_count
                             AND EXISTS (
                                 SELECT 1 FROM generation_artifacts a
                                 WHERE a.generation_id=d.generation_id AND a.document_id=d.document_id
                                   AND a.artifact_type='ocr_markdown' AND a.content_sha256=d.ocr_sha256
                             )
                       )::int AS latest_ocr,
                       count(*) FILTER (
                           WHERE d.is_latest AND d.structured_sha256 IS NOT NULL
                             AND d.structure_schema_version='structured-document.v1'
                             AND EXISTS (
                                 SELECT 1 FROM generation_artifacts a
                                 WHERE a.generation_id=d.generation_id AND a.document_id=d.document_id
                                   AND a.artifact_type='structured'
                                   AND a.content_sha256=d.structured_sha256
                             )
                       )::int AS latest_structure,
                       count(*) FILTER (
                           WHERE d.is_latest AND d.embedding_provider=%s AND d.embedding_model=%s
                             AND d.embedding_dimension=%s AND d.chunk_policy=%s
                             AND d.chunk_count > 0 AND d.embedding_count=d.chunk_count
                             AND EXISTS (
                                 SELECT 1 FROM generation_artifacts a
                                 WHERE a.generation_id=d.generation_id AND a.document_id=d.document_id
                                   AND a.artifact_type='embedding'
                             )
                       )::int AS latest_embedding,
                       count(*) FILTER (
                           WHERE d.is_latest AND d.index_count=d.chunk_count AND d.index_count > 0
                             AND d.index_count=(
                                 SELECT count(*)::int FROM evidence e
                                 WHERE e.generation_id=d.generation_id AND e.document_id=d.document_id
                                   AND e.embedding IS NOT NULL
                                   AND evidence_source_spans_valid(
                                       e.source_spans, e.page_start, e.page_end,
                                       e.span_start, e.span_end
                                   )
                             )
                             AND EXISTS (
                                 SELECT 1 FROM generation_artifacts a
                                 WHERE a.generation_id=d.generation_id AND a.document_id=d.document_id
                                   AND a.artifact_type='lexical_index'
                             )
                             AND EXISTS (
                                 SELECT 1 FROM generation_artifacts a
                                 WHERE a.generation_id=d.generation_id AND a.document_id=d.document_id
                                   AND a.artifact_type='vector_index'
                             )
                       )::int AS latest_index
                FROM generation_documents d WHERE d.generation_id=%s
                """,
                (
                    PDF_RENDERER_ID,
                    embedding_provider,
                    embedding_model,
                    dimension,
                    CHUNK_POLICY_VERSION,
                    generation_id,
                ),
            )
            counts = cursor.fetchone()
            if counts is None:
                raise RuntimeError("generation coverage query returned no row")
            cursor.execute(
                """
                SELECT count(*) FILTER (
                           WHERE EXISTS (
                               SELECT 1 FROM generation_documents d
                               JOIN source_documents source USING (document_id)
                               WHERE d.generation_id=%s AND d.is_latest
                                 AND (d.document_id=j.document_id OR source.discovery_id=j.document_id)
                           )
                       )::int AS latest_failed,
                       count(*) FILTER (
                           WHERE NOT EXISTS (
                               SELECT 1 FROM generation_documents d
                               JOIN source_documents source USING (document_id)
                               WHERE d.generation_id=%s AND d.is_latest
                                 AND (d.document_id=j.document_id OR source.discovery_id=j.document_id)
                           )
                       )::int AS historical_quarantine
                FROM jobs j
                WHERE j.state IN ('dead_letter','cancelled') AND j.document_id IS NOT NULL
                  AND j.payload->>'generation_id'=%s
                """,
                (generation_id, generation_id, generation_id),
            )
            failures = cursor.fetchone()
            if failures is None:
                raise RuntimeError("generation failure coverage query returned no row")
            cursor.execute(
                """
                SELECT count(*)::int AS expected,
                       count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM source_documents source
                           JOIN generation_documents d
                             ON d.generation_id=x.generation_id
                            AND d.document_id=source.document_id
                           WHERE source.discovery_id=x.discovery_id
                             AND d.source_snapshot_id=x.source_snapshot_id
                             AND d.is_latest
                       ))::int AS materialized
                FROM generation_expected_documents x
                WHERE x.generation_id=%s AND x.is_current
                """,
                (generation_id,),
            )
            expectations = cursor.fetchone()
            if expectations is None:
                raise RuntimeError("generation discovery expectation query returned no row")
            cursor.execute(
                """
                SELECT count(*)::int AS mismatched
                FROM generation_snapshots gs
                JOIN source_snapshots s ON s.snapshot_id=gs.snapshot_id
                LEFT JOIN LATERAL (
                    SELECT count(*)::int AS expected
                    FROM generation_expected_documents x
                    WHERE x.generation_id=gs.generation_id
                      AND x.source_snapshot_id=gs.snapshot_id
                ) x ON true
                WHERE gs.generation_id=%s AND x.expected<>s.observed_count
                """,
                (generation_id,),
            )
            snapshot_count = cursor.fetchone()
            if snapshot_count is None or int(snapshot_count["mismatched"]):
                raise ValueError("generation expectations do not reconcile to issuer snapshot counts")
        return {
            "source_snapshot_ids": [str(value) for value in generation["snapshot_ids"]],
            "snapshot_issuers": [str(value) for value in generation["snapshot_issuers"]],
            "all_documents": int(counts["all_documents"]),
            "latest_total": int(counts["latest_total"]),
            "latest_pdf": int(counts["latest_pdf"]),
            "latest_ocr": int(counts["latest_ocr"]),
            "latest_structure": int(counts["latest_structure"]),
            "latest_embedding": int(counts["latest_embedding"]),
            "latest_index": int(counts["latest_index"]),
            "current_expected": int(expectations["expected"]),
            "current_materialized": int(expectations["materialized"]),
            "latest_failed": int(failures["latest_failed"]),
            "historical_quarantine": int(failures["historical_quarantine"]),
        }

    def _is_no_change(
        self,
        generation_id: str,
        *,
        embedding_provider: str,
        embedding_model: str,
        dimension: int,
    ) -> bool:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.generation_id,
                       g.embedding_provider=%s AND g.embedding_model=%s
                           AND g.embedding_dimension=%s AS compatible,
                       NOT EXISTS (
                           (SELECT document_id, pdf_sha256, ocr_sha256, structured_sha256,
                                   ocr_manifest->'attempt'->>'prompt_version',
                                   ocr_manifest->'attempt'->>'reasoning_effort',
                                   ocr_manifest->'attempt'->>'provider',
                                   ocr_manifest->'attempt'->>'model',
                                   ocr_manifest->'attempt'->>'renderer',
                                   ocr_manifest->'attempt'->>'render_scale',
                                   ocr_manifest->'attempt'->>'chunk_pages',
                                   structure_schema_version, embedding_provider, embedding_model,
                                   embedding_dimension, chunk_policy, is_latest
                            FROM generation_documents
                            WHERE generation_id=%s
                            EXCEPT
                            SELECT document_id, pdf_sha256, ocr_sha256, structured_sha256,
                                   ocr_manifest->'attempt'->>'prompt_version',
                                   ocr_manifest->'attempt'->>'reasoning_effort',
                                   ocr_manifest->'attempt'->>'provider',
                                   ocr_manifest->'attempt'->>'model',
                                   ocr_manifest->'attempt'->>'renderer',
                                   ocr_manifest->'attempt'->>'render_scale',
                                   ocr_manifest->'attempt'->>'chunk_pages',
                                   structure_schema_version, embedding_provider, embedding_model,
                                   embedding_dimension, chunk_policy, is_latest
                            FROM generation_documents
                            WHERE generation_id=a.generation_id)
                           UNION ALL
                           (SELECT document_id, pdf_sha256, ocr_sha256, structured_sha256,
                                   ocr_manifest->'attempt'->>'prompt_version',
                                   ocr_manifest->'attempt'->>'reasoning_effort',
                                   ocr_manifest->'attempt'->>'provider',
                                   ocr_manifest->'attempt'->>'model',
                                   ocr_manifest->'attempt'->>'renderer',
                                   ocr_manifest->'attempt'->>'render_scale',
                                   ocr_manifest->'attempt'->>'chunk_pages',
                                   structure_schema_version, embedding_provider, embedding_model,
                                   embedding_dimension, chunk_policy, is_latest
                            FROM generation_documents
                            WHERE generation_id=a.generation_id
                            EXCEPT
                            SELECT document_id, pdf_sha256, ocr_sha256, structured_sha256,
                                   ocr_manifest->'attempt'->>'prompt_version',
                                   ocr_manifest->'attempt'->>'reasoning_effort',
                                   ocr_manifest->'attempt'->>'provider',
                                   ocr_manifest->'attempt'->>'model',
                                   ocr_manifest->'attempt'->>'renderer',
                                   ocr_manifest->'attempt'->>'render_scale',
                                   ocr_manifest->'attempt'->>'chunk_pages',
                                   structure_schema_version, embedding_provider, embedding_model,
                                   embedding_dimension, chunk_policy, is_latest
                            FROM generation_documents
                            WHERE generation_id=%s)
                       )
                       AND NOT EXISTS (
                           SELECT 1 FROM generation_documents d
                           WHERE d.generation_id=%s AND (
                               d.structure_schema_version IS DISTINCT FROM %s
                               OR d.chunk_policy IS DISTINCT FROM %s
                               OR d.ocr_manifest->'attempt'->>'prompt_version'
                                    IS DISTINCT FROM %s
                               OR d.ocr_manifest->'attempt'->>'renderer'
                                    IS DISTINCT FROM %s
                           )
                       ) AS same_documents
                FROM active_generation a JOIN generations g USING (generation_id)
                WHERE a.singleton=true AND a.generation_id<>%s
                """,
                (
                    embedding_provider,
                    embedding_model,
                    dimension,
                    generation_id,
                    generation_id,
                    generation_id,
                    STRUCTURE_SCHEMA_VERSION,
                    CHUNK_POLICY_VERSION,
                    OCR_PROMPT_VERSION,
                    PDF_RENDERER_ID,
                    generation_id,
                ),
            )
            row = cursor.fetchone()
        return bool(row is not None and row["compatible"] and row["same_documents"])

    @staticmethod
    def _validate_report_generation(
        generation_id: str,
        quality_report: QualityGateReport,
        retrieval_report: RetrievalGateReport,
    ) -> None:
        if quality_report.generation_id != generation_id:
            raise ValueError("quality report generation differs from candidate")
        if retrieval_report.generation_id != generation_id:
            raise ValueError("retrieval report generation differs from candidate")

    @staticmethod
    def _load_fixture_gate() -> tuple[FixtureQualityReport, str]:
        try:
            body = files("cardrag.resources").joinpath("fixture-gate.json").read_bytes()
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise ValueError("packaged fixture quality admission artifact is missing") from exc
        digest = hashlib.sha256(body).hexdigest()
        if digest != FIXTURE_GATE_REPORT_SHA256:
            raise ValueError("packaged fixture quality admission artifact was modified")
        report = FixtureQualityReport.model_validate_json(body)
        if report.schema_version != FIXTURE_GATE_SCHEMA:
            raise ValueError("fixture quality admission schema is incompatible")
        if report.fixture_set != FIXTURE_GATE_SET or report.fixture_sha256 != FIXTURE_GATE_GOLD_SHA256:
            raise ValueError("fixture quality admission gold provenance differs")
        if report.thresholds != QUALITY_THRESHOLDS or report.status != "passed":
            raise ValueError("fixture quality admission thresholds or status differ from ADR-0003")
        return report, digest

    @staticmethod
    def _validate_regression_binding(
        quality: QualityGateReport,
        retrieval: RetrievalGateReport,
        *,
        fixture_gate: FixtureQualityReport,
        fixture_gate_sha256: str,
    ) -> None:
        expected_quality = (
            fixture_gate_sha256,
            min(result.character_accuracy for result in fixture_gate.ocr.values()),
            min(result.page_coverage for result in fixture_gate.ocr.values()),
            all(result.page_order_exact for result in fixture_gate.ocr.values()),
            min(
                [result.critical_token_recall for result in fixture_gate.ocr.values()]
                + [result.critical_token_recall for result in fixture_gate.structure.values()]
            ),
            min(result.source_span_accuracy for result in fixture_gate.structure.values()),
            min(result.taxonomy_recall for result in fixture_gate.structure.values()),
            sum(result.critical_error_count for result in fixture_gate.ocr.values())
            + sum(result.critical_error_count for result in fixture_gate.structure.values()),
        )
        actual_quality = (
            quality.regression_report_sha256,
            quality.regression_character_accuracy,
            quality.regression_page_coverage,
            quality.regression_page_order_exact,
            quality.regression_critical_token_recall,
            quality.regression_source_span_accuracy,
            quality.regression_taxonomy_recall,
            quality.regression_critical_error_count,
        )
        expected_retrieval = (
            fixture_gate_sha256,
            fixture_gate.retrieval.query_count,
            fixture_gate.retrieval.recall_at_k,
            fixture_gate.retrieval.critical_recall_at_k,
            fixture_gate.retrieval.mean_reciprocal_rank,
            fixture_gate.retrieval.ndcg_at_k,
            fixture_gate.retrieval.filter_accuracy,
            fixture_gate.retrieval.issuer_collision_count,
        )
        actual_retrieval = (
            retrieval.regression_report_sha256,
            retrieval.query_count,
            retrieval.recall_at_10,
            retrieval.critical_recall_at_10,
            retrieval.mean_reciprocal_rank,
            retrieval.ndcg_at_10,
            retrieval.filter_accuracy,
            retrieval.issuer_collisions,
        )
        if actual_quality != expected_quality or actual_retrieval != expected_retrieval:
            raise ValueError("generation reports are not bound to the packaged regression evidence")

    def _mark_failed(self, generation_id: str, *, reason: str) -> None:
        with self.database.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE generations SET state='failed'
                WHERE generation_id=%s AND state IN ('building','validating')
                """,
                (generation_id,),
            )
            cursor.execute(
                """
                UPDATE pipeline_runs SET report=report || jsonb_build_object(
                    'generation_validation', 'failed', 'generation_reason', %s::text
                ) WHERE generation_id=%s
                """,
                (reason, generation_id),
            )
            connection.commit()
