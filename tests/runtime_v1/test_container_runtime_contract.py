from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
WORKER_PROVIDERS = (ROOT / "apps/cardrag-worker/src/cardrag_worker/providers.py").read_text(encoding="utf-8")
WORKER_OCR = (ROOT / "apps/cardrag-worker/src/cardrag_worker/ocr.py").read_text(encoding="utf-8")
FROM_STAGE = re.compile(r"^FROM \S+ AS (?P<name>[a-z0-9-]+)$", re.MULTILINE)


def _stage(name: str) -> str:
    matches = list(FROM_STAGE.finditer(DOCKERFILE))
    for index, match in enumerate(matches):
        if match.group("name") != name:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(DOCKERFILE)
        return DOCKERFILE[match.start() : end]
    raise AssertionError(f"Dockerfile stage is missing: {name}")


def test_runtime_sources_and_build_tool_are_digest_pinned() -> None:
    assert DOCKERFILE.startswith(
        "# syntax=docker/dockerfile:1.7@sha256:"
        "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720\n"
    )
    assert (
        "ARG PYTHON_DEV_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:"
        "4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2"
    ) in DOCKERFILE
    assert (
        "ARG PYTHON_RUNTIME_IMAGE=cgr.dev/chainguard/python:latest@sha256:"
        "f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332"
    ) in DOCKERFILE
    assert (
        "ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:"
        "e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1"
    ) in DOCKERFILE
    assert "FROM ${PYTHON_DEV_IMAGE} AS source" in DOCKERFILE
    assert "FROM ${PYTHON_DEV_IMAGE} AS worker-runtime" in DOCKERFILE
    assert "FROM ${PYTHON_RUNTIME_IMAGE} AS runtime" in DOCKERFILE
    assert 'test "$(uv --version)" = "uv 0.8.17"' in _stage("source")
    assert "UV_PYTHON=3.14" in _stage("source")
    assert "UV_PYTHON_DOWNLOADS=never" in _stage("source")
    assert "sys.version_info[:2] == (3, 14)" in _stage("source")
    assert "slim-bookworm" not in DOCKERFILE
    assert "4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2" not in DOCKERFILE
    assert "apt-get" not in DOCKERFILE


def test_mcp_final_is_minimal_nonroot_and_has_no_run_instruction() -> None:
    runtime = _stage("runtime")
    mcp = _stage("mcp")

    assert not re.search(r"^RUN\s", runtime, re.MULTILINE)
    assert not re.search(r"^RUN\s", mcp, re.MULTILINE)
    for shell_or_package_manager in ("apk ", "/bin/sh", "/bin/bash", "ash -c", "sh -c"):
        assert shell_or_package_manager not in runtime
        assert shell_or_package_manager not in mcp
    assert "COPY --from=runtime-layout /etc/passwd /etc/passwd" in runtime
    assert "COPY --from=runtime-layout /etc/group /etc/group" in runtime
    assert 'org.opencontainers.image.source="https://github.com/Kanu-Coffee/MCP_card_prd_detail"' in runtime
    assert 'org.opencontainers.image.version="${APP_VERSION}"' in runtime
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in runtime
    assert "USER 10001:10001" in runtime
    assert "FROM runtime AS mcp" in mcp
    assert "/var/lib/cardrag-mcp /var/lib/cardrag-mcp" in mcp
    assert 'org.opencontainers.image.title="CardRAG MCP"' in mcp
    assert "HEALTHCHECK --interval=30s" in mcp
    assert "http://127.0.0.1:8000/health/ready" in mcp
    assert 'ENTRYPOINT ["cardrag-mcp"]' in mcp


def test_worker_keeps_exact_wolfi_sandbox_and_codex_contract() -> None:
    layout = _stage("runtime-layout")
    worker_runtime = _stage("worker-runtime")
    worker = _stage("worker")

    assert "addgroup -S -g 10001 cardrag" in layout
    assert "adduser -S -D -H -u 10001 -G cardrag" in layout
    assert "/var/lib/cardrag-codex-home/home" in layout
    assert "chown -R 10001:10001 /var/lib/cardrag-worker" in layout
    assert "chmod 0700 /var/lib/cardrag-worker /var/lib/cardrag-codex-home" in layout
    assert "bubblewrap=0.11.2-r0" in worker_runtime
    assert "libcap=2.78-r0" in worker_runtime
    assert "apk upgrade" not in worker_runtime
    assert "--ignore-unfixed" not in DOCKERFILE
    assert "ARG CODEX_VERSION=0.151.0" in DOCKERFILE
    assert ("ARG CODEX_SHA256=605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6") in DOCKERFILE
    # The pinned Wolfi runtime provides BusyBox sha256sum, which implements the
    # POSIX-style short check flag but not GNU's --check/--strict long options.
    assert "sha256sum -c /tmp/codex.sha256" in worker_runtime
    assert "sha256sum --check --strict" not in worker_runtime
    assert "ln -s codex /usr/local/bin/codex-linux-sandbox" in worker_runtime
    assert "codex --version" in worker_runtime
    assert "USER 10001:10001" in worker_runtime
    assert "/var/lib/cardrag-worker /var/lib/cardrag-worker" in worker
    assert "/var/lib/cardrag-codex-home /var/lib/cardrag-codex-home" in worker
    assert 'VOLUME ["/var/lib/cardrag-worker", "/var/lib/cardrag-codex-home"]' in worker
    assert 'org.opencontainers.image.title="CardRAG Worker"' in worker
    assert 'ENTRYPOINT ["cardrag-worker"]' in worker
    assert 'CMD ["run"]' in worker

    for disabled_feature in (
        "shell_tool",
        "unified_exec",
        "shell_snapshot",
        "view_image",
        "apps",
        "plugins",
        "browser_use",
        "computer_use",
        "multi_agent",
        "workspace_dependencies",
    ):
        assert f'"{disabled_feature}"' in WORKER_PROVIDERS
    for fixed_argument in (
        '"--strict-config"',
        '"--ignore-user-config"',
        '"--ignore-rules"',
        'shell_environment_policy.inherit="none"',
        "allow_login_shell=false",
    ):
        assert fixed_argument in WORKER_PROVIDERS
    assert "reject_credential_bearing_ocr(stdout)" in WORKER_PROVIDERS
    assert WORKER_OCR.count("reject_credential_bearing_ocr") >= 6


def test_runtime_images_publish_apache_2_0_license_metadata() -> None:
    runtime = _stage("runtime")
    worker_runtime = _stage("worker-runtime")

    for stage in (runtime, worker_runtime):
        assert 'org.opencontainers.image.licenses="Apache-2.0"' in stage
        assert "COPY --chmod=0444 LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/cardrag/" in stage
    assert "Proprietary" not in DOCKERFILE


def test_runtime_docs_record_fail_closed_verification_gap() -> None:
    runtime_doc = (ROOT / "docs/V1_0_10_CONTAINER_RUNTIME.md").read_text(encoding="utf-8")
    worker_compose = (ROOT / "deploy/worker/compose.yaml").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    for required in (
        "bubblewrap=0.11.2-r0",
        "libcap=2.78-r0",
        "92e4644298be253e29f66793e087b5c77e569a6dd39b7d3238302508874a427d",
        "0a6dc8ec3226500e7a3546fb19bbcbc3afa270f77d7a91b9f47ee6c6318e7224",
        "ignore-unfixed",
        "allowlist",
        "final image의 0/0 증명이 아닙니다",
        "bin/python`은 `/usr/bin/python3.14",
        "service account를",
        "bubblewrap user-namespace smoke",
        "Codex read-only sandbox smoke",
        "seccomp=unconfined",
        "apparmor=unconfined",
        "systempaths=unconfined",
        'shell_environment_policy.inherit="none"',
        "credential token form",
        "native/adopted remote cache",
        "/health/ready",
    ):
        assert required in runtime_doc

    assert "seccomp=unconfined" in worker_compose
    assert "apparmor=unconfined" in worker_compose
    assert "systempaths=unconfined" not in worker_compose
    assert "privileged:" not in worker_compose
    assert "cap_add:" not in worker_compose

    assert "docker pull" in runtime_doc
    assert "docker build" in runtime_doc
    assert "전혀 수행하지 않았습니다" in runtime_doc
    assert "Wolfi `bubblewrap` and `libcap`" in notices
    assert "official signed Wolfi package index" in notices
    assert "Debian base-image packages" not in notices
