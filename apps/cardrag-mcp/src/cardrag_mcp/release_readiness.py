"""Release-readiness CLI: operational receipt validation with MCP response replay.

This offline entry point delegates evidence reading and generation/hash binding
to ``cardrag_core.release_readiness`` and reuses the same normalized MCP
response model replay that ``cardrag_mcp.candidate_smoke`` performs. Unlike the
v2 acceptance receipt path, no document-aggregation-profile binding is
required: that identity is a research output and this gate is functional.
"""

from __future__ import annotations

from collections.abc import Sequence

from cardrag_core import release_readiness

from cardrag_mcp.candidate_smoke import validate_tool_response


def main(argv: Sequence[str] | None = None) -> int:
    return release_readiness.main(argv, mcp_response_validator=validate_tool_response)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
