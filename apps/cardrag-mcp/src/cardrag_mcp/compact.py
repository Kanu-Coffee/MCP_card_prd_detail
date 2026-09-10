"""Atomic source groups and a conservative byte budget for compact tool results."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from cardrag_mcp.models import (
    CompactSearchDiagnostics,
    ContractEvidenceBundle,
    ContractSearchPage,
    StructureNode,
)

COMPACT_RESPONSE_BYTES = 65_536


def encoded_response_bytes(page: ContractSearchPage) -> int:
    """Bound pretty ASCII JSON as well as smaller compact UTF-8 representations."""

    return len(json.dumps(page.model_dump(mode="json"), ensure_ascii=True, indent=2).encode())


def clause_group(nodes: Sequence[StructureNode], initial_id: str) -> tuple[StructureNode, ...]:
    """Keep clause/table subtrees and semantic links, without walking sibling chains.

    Ancestor headings provide context but do not trigger unrelated branch expansion.
    APPLIES_TO is directional: include the condition constraining a selected target.
    Footnotes and continuations form an atomic group, including transitive links.
    """

    by_id = {node.node_id: node for node in nodes}
    seed = by_id.get(initial_id)
    if seed is None or seed.node_type in {"ROOT", "MAJOR_SECTION"}:
        return ()
    children: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        if node.parent_id is not None:
            children[node.parent_id].add(node.node_id)
    selected: set[str] = set()
    substantive: set[str] = set()

    def add(node_id: str) -> None:
        if node_id not in by_id:
            return
        current: str | None = node_id
        nearest_item: str | None = None
        nearest_table: str | None = None
        while current is not None and current in by_id:
            selected.add(current)
            node = by_id[current]
            if nearest_item is None and node.node_type == "ITEM":
                nearest_item = current
            if nearest_table is None and node.node_type == "TABLE":
                nearest_table = current
            current = node.parent_id
        pending = [nearest_item or nearest_table or node_id]
        while pending:
            current = pending.pop()
            if current in substantive:
                continue
            substantive.add(current)
            selected.add(current)
            pending.extend(children[current])

    add(initial_id)
    links = {link for node in nodes for link in node.links}
    while True:
        before = len(substantive)
        for link in links:
            if link.link_type == "APPLIES_TO" and link.to_node_id in selected:
                add(link.from_node_id)
            elif link.link_type in {"FOOTNOTE_OF", "CONTINUATION_OF"} and (
                link.from_node_id in substantive or link.to_node_id in substantive
            ):
                add(link.from_node_id)
                add(link.to_node_id)
        if len(substantive) == before:
            break
    return tuple(
        node.model_copy(
            update={
                "links": tuple(
                    link
                    for link in node.links
                    if link.from_node_id in selected and link.to_node_id in selected
                )
            }
        )
        for node in nodes
        if node.node_id in selected
    )


def _merge(
    bundles: Sequence[ContractEvidenceBundle], group: ContractEvidenceBundle
) -> tuple[ContractEvidenceBundle, ...]:
    result = list(bundles)
    for index, previous in enumerate(result):
        if previous.contract.contract_revision_id != group.contract.contract_revision_id:
            continue
        nodes = {node.node_id: node for node in previous.nodes}
        for node in group.nodes:
            if node.node_id in nodes:
                old = nodes[node.node_id]
                node = node.model_copy(
                    update={
                        "links": tuple(
                            sorted(
                                set(old.links) | set(node.links),
                                key=lambda link: (
                                    link.from_node_id,
                                    link.to_node_id,
                                    link.link_type,
                                ),
                            )
                        )
                    }
                )
            nodes[node.node_id] = node
        matches = {match.node.node_id: match for match in previous.matches}
        matches.update({match.node.node_id: match for match in group.matches})
        merged_nodes = tuple(sorted(nodes.values(), key=lambda node: (node.ordinal, node.node_id)))
        linked_notice_ids = {
            node_id
            for node in merged_nodes
            for link in node.links
            if link.link_type == "APPLIES_TO"
            for node_id in (link.from_node_id, link.to_node_id)
            if nodes[node_id].major_class in {"NOTICE", "MIXED"}
        }
        result[index] = ContractEvidenceBundle(
            contract=previous.contract,
            matches=tuple(
                match.model_copy(update={"node": nodes[match.node.node_id]})
                for match in matches.values()
            ),
            nodes=merged_nodes,
            linked_notice_count=len(linked_notice_ids),
            parent_expansion_count=max(0, len(nodes) - len(matches)),
        )
        return tuple(result)
    return (*result, group)


def seal_compact_page(
    page: ContractSearchPage,
    groups: Sequence[ContractEvidenceBundle],
    *,
    ranked_candidate_contracts: int,
    coarse_count: int,
    candidate_limit_count: int,
) -> ContractSearchPage:
    """Admit complete groups only; expose omissions even when no group fits."""

    candidates = len(groups) + coarse_count + candidate_limit_count

    def build(
        bundles: tuple[ContractEvidenceBundle, ...], accepted: int, omitted: list[str]
    ) -> ContractSearchPage:
        reasons: list[Literal["response_byte_limit", "candidate_limit", "coarse_view"]] = []
        if omitted:
            reasons.append("response_byte_limit")
        if candidate_limit_count or ranked_candidate_contracts > len(
            {group.contract.contract_revision_id for group in groups}
        ):
            reasons.append("candidate_limit")
        if coarse_count:
            reasons.append("coarse_view")
        diagnostics = CompactSearchDiagnostics(
            response_bytes=0,
            ranked_candidate_contracts=ranked_candidate_contracts,
            candidate_clause_groups=candidates,
            returned_clause_groups=accepted,
            omitted_clause_groups=candidates - accepted,
            omitted_contracts=ranked_candidate_contracts - len(bundles),
            omission_reasons=tuple(reasons),
            omitted_group_sample=tuple(omitted[:8]),
            omitted_group_sample_complete=(
                len(omitted) <= 8
                and not coarse_count
                and not candidate_limit_count
                and ranked_candidate_contracts
                <= len({group.contract.contract_revision_id for group in groups})
            ),
        )
        coverage = page.coverage.model_copy(
            update={
                "response_node_count": sum(len(bundle.nodes) for bundle in bundles),
                "response_character_count": sum(
                    bundle.context_character_count for bundle in bundles
                ),
                "response_truncated": bool(candidates - accepted)
                or ranked_candidate_contracts > len(bundles),
                "lexical_additional_evidence_count": sum(
                    match.lexical_only for bundle in bundles for match in bundle.matches
                ),
                "full_contract_fallback_count": 0,
            }
        )
        result = ContractSearchPage(
            generation_id=page.generation_id,
            bundles=bundles,
            coverage=coverage,
            compact=diagnostics,
        )
        # The decimal byte count contributes to its own encoded size.
        for _ in range(6):
            size = encoded_response_bytes(result)
            current = result.compact
            if current is None:
                raise RuntimeError("compact diagnostics disappeared")
            if size == current.response_bytes:
                break
            result = result.model_copy(
                update={"compact": current.model_copy(update={"response_bytes": size})}
            )
        return result

    bundles: tuple[ContractEvidenceBundle, ...] = ()
    accepted = 0
    omitted: list[str] = []
    # Reserve the worst-case omission sample and diagnostics before admitting data.
    reserve = [
        f"{group.contract.contract_revision_id}/{group.matches[0].node.node_id}" for group in groups
    ]
    reserve = sorted(reserve, key=lambda value: len(json.dumps(value)), reverse=True)[:8]
    for group in groups:
        proposed = _merge(bundles, group)
        trial = build(proposed, accepted + 1, reserve)
        if encoded_response_bytes(trial) > COMPACT_RESPONSE_BYTES:
            omitted.append(f"{group.contract.contract_revision_id}/{group.matches[0].node.node_id}")
            continue
        bundles = proposed
        accepted += 1
    result = build(bundles, accepted, omitted)
    if encoded_response_bytes(result) > COMPACT_RESPONSE_BYTES:
        raise RuntimeError("compact response metadata exceeds the sealed byte budget")
    return ContractSearchPage.model_validate(result.model_dump())
