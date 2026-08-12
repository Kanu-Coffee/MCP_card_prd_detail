#!/usr/bin/env python3
"""Run the license-safe three-issuer quality gate and write its JSON report."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from cardrag.domain import Issuer
from cardrag.pipeline.chunks import build_chunks
from cardrag.pipeline.ocr import FakeOCRBackend, OCRProcessor, RenderedDocument, split_pages
from cardrag.pipeline.structure import SectionType, StructuredDocument, extract_structure
from cardrag.quality import (
    GoldQuery,
    QualityThresholds,
    RankedEvidence,
    evaluate_ocr,
    evaluate_retrieval,
    evaluate_structure,
    new_fixture_report,
)
from cardrag.search.hybrid import HybridSearchEngine, SearchFilters

_FIXTURE_GENERATION = "fixture-generation"
_VECTOR_DIMENSION = 384


def _feature_vector(text: str) -> list[float]:
    """Deterministic lexical/semantic stand-in used by the fixture pipeline.

    Word features retain amounts and Korean terms; character bigrams make
    ``교통`` match ``대중교통`` without a language-specific tokenizer.  This is
    deliberately not a claim about a live embedding provider.
    """

    normalized = re.sub(r"[^0-9a-z가-힣%]+", " ", text.casefold()).strip()
    compact = normalized.replace(" ", "")
    features = normalized.split()
    features.extend(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
    vector = [0.0] * _VECTOR_DIMENSION
    for feature in features:
        digest = hashlib.sha256(feature.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % _VECTOR_DIMENSION
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class _FixtureEmbedder:
    provider = "deterministic-fixture"
    model = "hashed-korean-bigram-v1"
    dimension = _VECTOR_DIMENSION

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_feature_vector(text) for text in texts]

    async def embed_query(self, query: str) -> list[float]:
        return _feature_vector(query)


class _FixtureSearchStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def active_generation_id(self) -> str:
        return _FIXTURE_GENERATION

    @staticmethod
    def _matches(row: dict[str, Any], filters: SearchFilters) -> bool:
        return (
            (filters.issuer is None or row["issuer"] == filters.issuer)
            and (filters.product_code is None or row["product_code"] == filters.product_code)
            and (filters.section_type is None or row["section_type"] == filters.section_type)
            and (filters.version is None or row["source_version"] == filters.version)
            and (filters.as_of is None or row["effective_date"] <= filters.as_of)
        )

    async def lexical_candidates(
        self,
        generation_id: str,
        query: str,
        filters: SearchFilters,
        limit: int,
    ) -> list[dict[str, Any]]:
        assert generation_id == _FIXTURE_GENERATION
        query_vector = _feature_vector(query)
        scored = [
            (sum(a * b for a, b in zip(query_vector, row["fixture_vector"], strict=True)), row)
            for row in self.rows
            if self._matches(row, filters)
        ]
        return [
            {**row, "branch_score": score}
            for score, row in sorted(scored, key=lambda item: (-item[0], item[1]["evidence_id"]))
            if score > 0
        ][:limit]

    async def vector_candidates(
        self,
        generation_id: str,
        vector: list[float],
        filters: SearchFilters,
        limit: int,
    ) -> list[dict[str, Any]]:
        assert generation_id == _FIXTURE_GENERATION
        scored = [
            (sum(a * b for a, b in zip(vector, row["fixture_vector"], strict=True)), row)
            for row in self.rows
            if self._matches(row, filters)
        ]
        return [
            {**row, "branch_score": score}
            for score, row in sorted(scored, key=lambda item: (-item[0], item[1]["evidence_id"]))
        ][:limit]


async def _exercise_fixture(
    fixture: dict[str, Any],
) -> tuple[dict[str, str], dict[str, StructuredDocument], dict[str, tuple[RankedEvidence, ...]]]:
    """Run fake OCR, production structure/chunking, embedding and hybrid search."""

    observed_ocr: dict[str, str] = {}
    structured_documents: dict[str, StructuredDocument] = {}
    rows: list[dict[str, Any]] = []
    alias_matches: dict[str, list[str]] = {str(alias): [] for alias in fixture["evidence"]}
    with tempfile.TemporaryDirectory(prefix="cardrag-fixture-gate-") as temporary:
        root = Path(temporary)
        for document in fixture["documents"]:
            key = str(document["key"])
            canonical = str(document["ocr"])
            canonical_pages = split_pages(canonical)
            page_bodies = {
                index: "\n".join(page.splitlines()[1:]).strip()
                for index, page in enumerate(canonical_pages, 1)
            }
            images: list[Path] = []
            for page in range(1, len(canonical_pages) + 1):
                image = root / key / "rendered" / f"page-{page:04d}.png"
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(f"synthetic-render:{key}:{page}".encode())
                images.append(image)
            rendered = RenderedDocument(
                pdf_sha256=hashlib.sha256(("synthetic-pdf:" + key).encode()).hexdigest(),
                page_images=tuple(images),
                render_scale=3.0,
            )
            await OCRProcessor(chunk_pages=2).process(
                document_id=key,
                rendered=rendered,
                output_dir=root / key / "ocr",
                primary=FakeOCRBackend(page_bodies),
                bulk=True,
            )
            observed = (root / key / "ocr" / "ocr.md").read_text(encoding="utf-8")
            observed_ocr[key] = observed
            structured = extract_structure(key, observed)
            structured_documents[key] = structured
            chunks = build_chunks(
                structured,
                issuer=Issuer(str(document["issuer"])),
                product_code=str(document["product_code"]),
                product_name=str(document["product_name"]),
                document_version=str(document["source_version"]),
                effective_date=str(document["effective_date"]),
                ocr_text=observed,
            )
            pdf_sha256 = hashlib.sha256(("synthetic-pdf:" + key).encode()).hexdigest()
            for chunk in chunks:
                vector = _feature_vector(chunk.text)
                rows.append(
                    {
                        **chunk.model_dump(mode="python"),
                        "generation_id": _FIXTURE_GENERATION,
                        "source_version": chunk.document_version,
                        "document_type": "product_description",
                        "effective_date": date.fromisoformat(chunk.effective_date),
                        "pdf_sha256": pdf_sha256,
                        "confidence": 1.0,
                        "fixture_vector": vector,
                    }
                )
                for alias, metadata in fixture["evidence"].items():
                    if (
                        metadata["issuer"] == chunk.issuer.value
                        and metadata["product_code"] == chunk.product_code
                        and metadata["source_version"] == chunk.document_version
                        and metadata["section_type"] == chunk.section_type
                        and str(metadata["must_contain"]) in chunk.text
                    ):
                        alias_matches[str(alias)].append(chunk.evidence_id)

    ambiguous = {alias: values for alias, values in alias_matches.items() if len(values) != 1}
    if ambiguous:
        raise ValueError(f"fixture evidence anchors must resolve exactly once: {ambiguous}")
    alias_to_actual = {alias: values[0] for alias, values in alias_matches.items()}
    engine = HybridSearchEngine(_FixtureSearchStore(rows), _FixtureEmbedder(), maximum_candidates=250)
    rankings: dict[str, tuple[RankedEvidence, ...]] = {}
    for raw_query in fixture["queries"]:
        query = GoldQuery.model_validate(
            {
                **raw_query,
                "relevant_evidence_ids": [
                    alias_to_actual[str(alias)] for alias in raw_query["relevant_evidence_ids"]
                ],
            }
        )
        result = await engine.search(
            query.query,
            filters=SearchFilters(
                issuer=query.issuer,
                product_code=query.product_code,
                section_type=query.section_type,
                version=query.source_version,
            ),
            limit=10,
        )
        rankings[query.query_id] = tuple(
            RankedEvidence(
                evidence_id=hit.evidence_id,
                issuer=hit.issuer,
                product_code=hit.product_code,
                source_version=hit.source_version,
                section_type=hit.section_type,
            )
            for hit in result.hits
        )
    # Return queries with their real evidence IDs so evaluation cannot replay
    # the fixture's expected order as observed output.
    fixture["queries"] = [
        {
            **query,
            "relevant_evidence_ids": [
                alias_to_actual[str(alias)] for alias in query["relevant_evidence_ids"]
            ],
        }
        for query in fixture["queries"]
    ]
    return observed_ocr, structured_documents, rankings


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(fixture_path: Path, output_path: Path) -> str:
    fixture_bytes = fixture_path.read_bytes()
    fixture = json.loads(fixture_bytes)
    thresholds = QualityThresholds()
    observed_ocr, structured_documents, rankings = asyncio.run(_exercise_fixture(fixture))
    ocr_results = {}
    structure_results = {}
    for document in fixture["documents"]:
        key = str(document["key"])
        canonical = str(document["ocr"])
        ocr_results[key] = evaluate_ocr(
            canonical,
            observed_ocr[key],
            thresholds=thresholds,
        )
        structured = structured_documents[key]
        structure_results[key] = evaluate_structure(
            canonical,
            structured,
            expected_section_types=tuple(SectionType(value) for value in document["expected_section_types"]),
            thresholds=thresholds,
        )

    queries = tuple(GoldQuery.model_validate(query) for query in fixture["queries"])
    retrieval = evaluate_retrieval(queries, rankings, k=10, thresholds=thresholds)
    report = new_fixture_report(
        fixture_set=str(fixture["fixture_set"]),
        fixture_sha256=hashlib.sha256(fixture_bytes).hexdigest(),
        ocr=ocr_results,
        structure=structure_results,
        retrieval=retrieval,
        thresholds=thresholds,
    )
    _atomic_json(output_path, report.model_dump(mode="json"))
    return report.status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/gold/gold_set.v1.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/quality/fixture-gate.json"),
    )
    args = parser.parse_args()
    status = run(args.fixture.resolve(), args.output.resolve())
    print(f"fixture quality gate: {status}; report={args.output}")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
