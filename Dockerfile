# syntax=docker/dockerfile:1.7@sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720

ARG PYTHON_DEV_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2
ARG PYTHON_RUNTIME_IMAGE=cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1
ARG CODEX_VERSION=0.147.0
ARG CODEX_SHA256=0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_DEV_IMAGE} AS source

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.14 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/cardrag

# Build stages require root to create the shared /opt/cardrag environment.
# hadolint ignore=DL3002
USER 0
WORKDIR /workspace
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY packages/cardrag-core ./packages/cardrag-core
COPY apps/cardrag-worker ./apps/cardrag-worker
COPY apps/cardrag-mcp ./apps/cardrag-mcp
RUN test "$(uv --version)" = "uv 0.8.17" && \
    python -c 'import sys; assert sys.version_info[:2] == (3, 14)'


FROM source AS worker-build
RUN uv sync --frozen --no-dev --no-editable --package cardrag-worker


FROM source AS mcp-build
RUN uv sync --frozen --no-dev --no-editable --package cardrag-mcp


FROM ${PYTHON_DEV_IMAGE} AS runtime-layout
USER 0
RUN addgroup -S -g 10001 cardrag && \
    adduser -S -D -H -u 10001 -G cardrag -h /nonexistent -s /sbin/nologin cardrag && \
    mkdir -p /usr/share/doc/cardrag /var/lib/cardrag-worker /var/lib/cardrag-mcp && \
    chown 10001:10001 /var/lib/cardrag-worker /var/lib/cardrag-mcp && \
    chmod 0755 /usr/share/doc/cardrag && \
    chmod 0700 /var/lib/cardrag-worker /var/lib/cardrag-mcp
USER 10001:10001


# The MCP runtime is intentionally shell- and package-manager-free. Keep this
# stage and every descendant free of RUN instructions.
FROM ${PYTHON_RUNTIME_IMAGE} AS runtime

ARG APP_VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/Kanu-Coffee/MCP_card_prd_detail" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/opt/cardrag/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

COPY --from=runtime-layout /etc/passwd /etc/passwd
COPY --from=runtime-layout /etc/group /etc/group
COPY --from=runtime-layout /usr/share/doc/cardrag /usr/share/doc/cardrag
COPY --chmod=0444 LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/cardrag/

WORKDIR /app
USER 10001:10001


FROM ${PYTHON_DEV_IMAGE} AS worker-runtime
ARG CODEX_VERSION
ARG CODEX_SHA256
ARG APP_VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/Kanu-Coffee/MCP_card_prd_detail" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/opt/cardrag/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

USER 0
# Exact versions make disappearance from Wolfi's signed rolling index fail the
# build instead of silently upgrading either side of bwrap's dynamic ABI.
RUN apk add --no-cache \
      bubblewrap=0.11.2-r0 \
      libcap=2.78-r0
COPY --from=runtime-layout /etc/passwd /etc/passwd
COPY --from=runtime-layout /etc/group /etc/group
COPY --from=runtime-layout /usr/share/doc/cardrag /usr/share/doc/cardrag
COPY --chmod=0444 LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/cardrag/
ADD --chmod=0644 \
  "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
  /tmp/codex.tar.gz
RUN printf '%s  %s\n' "${CODEX_SHA256}" /tmp/codex.tar.gz > /tmp/codex.sha256 && \
    sha256sum -c /tmp/codex.sha256 && \
    tar --extract --gzip --file /tmp/codex.tar.gz --directory /usr/local/bin && \
    mv /usr/local/bin/codex-x86_64-unknown-linux-musl /usr/local/bin/codex && \
    chmod 0755 /usr/local/bin/codex && \
    ln -s codex /usr/local/bin/codex-linux-sandbox && \
    rm /tmp/codex.tar.gz /tmp/codex.sha256 && \
    codex --version

WORKDIR /app
USER 10001:10001


FROM worker-runtime AS worker
LABEL org.opencontainers.image.title="CardRAG Worker" \
      org.opencontainers.image.description="One-shot card PDF, OCR, embedding, and WebDAV publisher"
COPY --from=worker-build /opt/cardrag /opt/cardrag
COPY --from=runtime-layout --chown=10001:10001 \
  /var/lib/cardrag-worker /var/lib/cardrag-worker
VOLUME ["/var/lib/cardrag-worker"]
ENTRYPOINT ["cardrag-worker"]
CMD ["run"]


FROM runtime AS mcp
LABEL org.opencontainers.image.title="CardRAG MCP" \
      org.opencontainers.image.description="Read-only SQLite CardRAG MCP synchronized from WebDAV"
ENV OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1
COPY --from=mcp-build /opt/cardrag /opt/cardrag
COPY --from=runtime-layout --chown=10001:10001 \
  /var/lib/cardrag-mcp /var/lib/cardrag-mcp
VOLUME ["/var/lib/cardrag-mcp"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).status==200 else 1)"]
ENTRYPOINT ["cardrag-mcp"]
