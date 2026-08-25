# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1
ARG CODEX_VERSION=0.147.0
ARG CODEX_SHA256=0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS source

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/cardrag

WORKDIR /workspace
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY packages/cardrag-core ./packages/cardrag-core
COPY apps/cardrag-worker ./apps/cardrag-worker
COPY apps/cardrag-mcp ./apps/cardrag-mcp


FROM source AS worker-build
RUN uv sync --frozen --no-dev --no-editable --package cardrag-worker


FROM source AS mcp-build
RUN uv sync --frozen --no-dev --no-editable --package cardrag-mcp


FROM ${PYTHON_IMAGE} AS runtime

ARG APP_VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.source="https://github.com/Kanu-Coffee/MCP_card_prd_detail" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Proprietary"

ENV PATH="/opt/cardrag/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

RUN groupadd --gid 10001 cardrag && \
    useradd --uid 10001 --gid cardrag --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin cardrag && \
    install --directory --owner=root --group=root --mode=0755 /usr/share/doc/cardrag && \
    install --directory --owner=10001 --group=10001 --mode=0700 \
      /var/lib/cardrag-worker /var/lib/cardrag-mcp

COPY --chmod=0444 THIRD_PARTY_NOTICES.md /usr/share/doc/cardrag/THIRD_PARTY_NOTICES.md

WORKDIR /app
USER 10001:10001


FROM runtime AS worker-runtime
ARG CODEX_VERSION
ARG CODEX_SHA256
USER 0
ADD --chmod=0644 \
  "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
  /tmp/codex.tar.gz
RUN apt-get update && \
    apt-get install --yes --no-install-recommends bubblewrap=0.8.0-2+deb12u1 && \
    rm -rf /var/lib/apt/lists/* && \
    printf '%s  %s\n' "${CODEX_SHA256}" /tmp/codex.tar.gz > /tmp/codex.sha256 && \
    sha256sum --check --strict /tmp/codex.sha256 && \
    tar --extract --gzip --file /tmp/codex.tar.gz --directory /usr/local/bin && \
    mv /usr/local/bin/codex-x86_64-unknown-linux-musl /usr/local/bin/codex && \
    chmod 0755 /usr/local/bin/codex && \
    ln -s codex /usr/local/bin/codex-linux-sandbox && \
    rm /tmp/codex.tar.gz /tmp/codex.sha256 && \
    codex --version
USER 10001:10001


FROM worker-runtime AS worker
LABEL org.opencontainers.image.title="CardRAG Worker" \
      org.opencontainers.image.description="One-shot card PDF, OCR, embedding, and WebDAV publisher"
COPY --from=worker-build /opt/cardrag /opt/cardrag
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
VOLUME ["/var/lib/cardrag-mcp"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).status==200 else 1)"]
ENTRYPOINT ["cardrag-mcp"]
