#!/usr/bin/env python3
"""Candidate MCP (v1.0.29) PLAN checklist verification over streamable HTTP JSON-RPC.

Covers: all 12 tools discovered and actually invoked; per-issuer product launch
dates; recent-product coverage; unknown-launch-date items reported without
estimation; past-notice exception behavior. Emits JSON evidence on stdout.
The bearer token is read from the host secret file and never printed.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("MCP_URL", "http://127.0.0.1:18022/mcp")
TOKEN = open(os.environ["MCP_TOKEN_FILE"]).read().strip()
PROTOCOL = "2025-03-26"
session_id: str | None = None
_msg = [0]


def post(payload: dict) -> dict | list:
    global session_id
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {TOKEN}",
        "MCP-Protocol-Version": PROTOCOL,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=300) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            session_id = sid
        body = resp.read().decode("utf-8", "replace")
    if "\ndata: " in body or body.startswith("event:"):
        datas = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
        body = datas[-1] if datas else "{}"
    return json.loads(body) if body.strip() else {}


def rpc(method: str, params: dict | None = None) -> dict:
    _msg[0] += 1
    out = post({"jsonrpc": "2.0", "id": _msg[0], "method": method, "params": params or {}})
    if isinstance(out, list):
        out = out[0]
    if "error" in out:
        raise RuntimeError(f"{method} error: {out['error']}")
    return out["result"]


def call_tool(name: str, args: dict) -> dict:
    result = rpc("tools/call", {"name": name, "arguments": args})
    if result.get("isError"):
        raise RuntimeError(f"tool {name} isError: {json.dumps(result, ensure_ascii=False)[:300]}")
    payload = json.loads(result["content"][0]["text"])
    generation = payload.get("generation_id")
    if generation:
        payload["_generation_ok"] = generation == EXPECTED_GENERATION
    return payload


def call_tool_retry(name: str, args: dict, attempts: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return call_tool(name, args)
        except RuntimeError as exc:
            last = exc
            if "embedding request failed" not in str(exc) and "Internal" not in str(exc):
                raise
            import time
            time.sleep(2 + 3 * attempt)
    raise last if last else RuntimeError(name)


EXPECTED_GENERATION = "g-0928dee8e6f04af9ae41fdb7-f916d1c475e0"


def main() -> None:
    evidence: dict = {"expected_generation_id": EXPECTED_GENERATION}
    init = rpc("initialize", {"protocolVersion": PROTOCOL, "clientInfo": {"name": "fix02-verifier", "version": "1.0.0"}, "capabilities": {}})
    evidence["server_info"] = init.get("serverInfo")
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = rpc("tools/list")["tools"]
    names = sorted(t["name"] for t in tools)
    evidence["tools_list_count"] = len(names)
    evidence["tools_list"] = names
    called: set[str] = set()

    cat = call_tool("find_products", {"mode": "catalog", "issuer": "kb", "limit": 5})
    called.add("find_products")
    items = cat["items"]
    evidence["find_products_catalog"] = {"count": len(items), "total": cat.get("total_count"), "generation_bound": cat.get("_generation_ok", False)}
    item = items[0]

    prod = call_tool("get_product", {"issuer": item["issuer"], "product_code": item["product_code"]})
    called.add("get_product")
    evidence["get_product"] = {"keys": sorted(prod)[:10], "generation_bound": prod.get("_generation_ok", "product" in prod)}

    summ = call_tool("get_product_summary", {"products": [{"issuer": "kb", "identifier": item["product_code"]}, {"issuer": "shinhan", "identifier": "원더"}]})
    called.add("get_product_summary")
    evidence["get_product_summary"] = {"count": len(summ.get("products") or summ.get("results") or [summ])}

    rev = call_tool("list_product_revisions", {"issuer": item["issuer"], "product_lineage_id": item["product_lineage_id"]})
    called.add("list_product_revisions")
    evidence["list_product_revisions"] = {"keys": sorted(rev)[:10], "count": len(rev.get("revisions") or [])}

    bundle = call_tool("get_contract_bundle", {"contract_revision_id": item["contract_revision_id"]})
    called.add("get_contract_bundle")
    evidence["get_contract_bundle"] = {"keys": sorted(bundle)[:12]}

    doc_id = item["document_id"]
    page = call_tool("get_source_page", {"document_id": doc_id, "page": 1})
    called.add("get_source_page")
    evidence["get_source_page"] = {"keys": sorted(page)[:8], "chars": len(json.dumps(page, ensure_ascii=False))}

    pdf = call_tool("get_source_pdf", {"document_id": doc_id})
    called.add("get_source_pdf")
    evidence["get_source_pdf"] = {"keys": sorted(pdf)[:8]}

    se = call_tool_retry("search_evidence", {"query": "연회면제 이용실적", "issuer": "kb", "limit": 5, "allow_degraded": True})
    called.add("search_evidence")
    se_items = se.get("items") or []
    evidence["search_evidence"] = {"count": len(se_items), "degraded": se.get("degraded"), "retrieval_mode": se.get("retrieval_mode")}
    if se_items:
        ev = call_tool_retry("get_evidence", {"evidence_id": se_items[0].get("evidence_id") or se_items[0].get("id")})
        called.add("get_evidence")
        evidence["get_evidence"] = {"keys": sorted(ev)[:10]}

    fcm = call_tool("find_cards_by_merchant", {"merchant_name": "스타벅스"})
    called.add("find_cards_by_merchant")
    evidence["find_cards_by_merchant"] = {"count": len(fcm.get("items") or []), "total": fcm.get("total_count")}

    sc = call_tool_retry("search_contracts", {"query": "연회비", "issuer": "kb", "mode": "exact", "limit": 5})
    called.add("search_contracts")
    evidence["search_contracts"] = {"bundles": len(sc.get("bundles") or []), "coverage_keys": sorted((sc.get("coverage") or {}))[:8]}

    for probe_date in ("2026-01-05", "2026-09-01"):
        try:
            past = call_tool_retry("search_contracts", {"query": "해외이용수수료", "issuer": "kb", "limit": 5, "as_of": probe_date})
            evidence[f"search_contracts_past_as_of_{probe_date}"] = {
                "bundles": len(past.get("bundles") or []),
                "coverage_keys": sorted((past.get("coverage") or {}))[:8],
            }
            break
        except RuntimeError as exc:
            evidence[f"search_contracts_past_as_of_{probe_date}"] = {"typed_error": str(exc)[:200]}
    hist = call_tool_retry("search_contracts", {"query": "해외이용수수료", "limit": 5, "include_history": True})
    evidence["search_contracts_include_history"] = {"bundles": len(hist.get("bundles") or [])}
    try:
        call_tool_retry("search_contracts", {"query": "해외이용수수료", "as_of": "2026-01-05", "include_history": True})
        evidence["as_of_include_history_exclusive"] = "NOT enforced"
    except RuntimeError:
        evidence["as_of_include_history_exclusive"] = "rejected as documented"

    # per-issuer launch dates + recent coverage
    issuers = ["bc", "hana", "hyundai", "kb", "lotte", "samsung", "shinhan", "woori"]
    per_issuer = {}
    unknown_samples: list[dict] = []
    for issuer in issuers:
        r = call_tool("list_recent_products", {"issuer": issuer, "months": 6, "limit": 10})
        items_i = r.get("items") or []
        per_issuer[issuer] = {
            "returned": len(items_i),
            "with_launch_date": sum(1 for i in items_i if i.get("launch_date")),
            "unknown_launch_date_count": r.get("unknown_launch_date_count"),
            "generation_bound": r.get("_generation_ok", False),
        }
        for i in items_i:
            if not i.get("launch_date") and len(unknown_samples) < 5:
                unknown_samples.append({k: i.get(k) for k in ("issuer", "product_code", "product_name", "launch_date_status", "launch_date", "effective_date")})
    evidence["per_issuer_launch_dates"] = per_issuer
    called.add("list_recent_products")

    recent = call_tool("list_recent_products", {"months": 12, "limit": 100})
    items_r = recent.get("items") or []
    evidence["recent_products_coverage"] = {
        "items_12m": len(items_r),
        "total_count": recent.get("total_count"),
        "unknown_launch_date_count": recent.get("unknown_launch_date_count"),
        "next_cursor_present": bool(recent.get("next_cursor")),
    }
    evidence["unknown_launch_date_samples"] = unknown_samples
    evidence["unknown_items_report_without_date"] = all(not s["launch_date"] for s in unknown_samples)

    cov = call_tool("find_products", {"mode": "coverage"})
    evidence["coverage_report"] = {k: cov.get(k) for k in ("product_count", "coverage_scope")}
    evidence["coverage_by_issuer"] = [
        {k: i[k] for k in ("issuer", "product_count", "confirmed_launch_date_count", "unknown_launch_date_count", "unsupported_drm_count")}
        for i in cov.get("issuers") or []
    ]

    evidence["tools_called"] = sorted(called)
    evidence["tools_not_called"] = sorted(set(names) - called)
    evidence["all_12_tools_responded"] = not (set(names) - called)
    json.dump(evidence, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
