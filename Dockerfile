# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG POSTGRES_CLIENT_IMAGE=pgvector/pgvector:0.8.6-pg17-bookworm@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f
ARG CODEX_VERSION=0.147.0
ARG CODEX_SHA256=0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36

FROM ${PYTHON_IMAGE} AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/cardrag \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy

WORKDIR /build

RUN python -m venv /opt/cardrag

COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 \
  /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable --active

COPY legal ./legal
COPY scripts/check_dependency_licenses.py ./scripts/check_dependency_licenses.py
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md

RUN /opt/cardrag/bin/python scripts/check_dependency_licenses.py \
      --notices-only \
      --release \
      --notice-output-root /tmp/cardrag-license-notices \
      --output /tmp/cardrag-license-report.json


# Use the exact PostgreSQL patch release deployed by Compose. Only the
# owner-only admin target receives these clients; MCP and worker images do not
# need database backup authority or binaries.
FROM ${POSTGRES_CLIENT_IMAGE} AS postgres_client


FROM ${PYTHON_IMAGE} AS runtime

ARG APP_VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="CardRAG MCP" \
      org.opencontainers.image.description="Evidence-first CardRAG MCP and offline worker" \
      org.opencontainers.image.source="https://github.com/Kanu-Coffee/MCP_card_prd_detail" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="Proprietary"

ENV PATH="/opt/cardrag/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CARDRAG_APPLICATION_VERSION="${APP_VERSION}" \
    CARDRAG_IMAGE_REVISION="${VCS_REF}" \
    HOME=/var/lib/cardrag-home \
    TZ=Asia/Seoul

RUN groupadd --gid 10001 cardrag && \
    useradd --uid 10001 --gid cardrag --no-create-home --home-dir /var/lib/cardrag-home \
      --shell /usr/sbin/nologin cardrag && \
    install --directory --owner=10001 --group=10001 --mode=0700 /var/lib/cardrag-home

COPY --from=build /opt/cardrag /opt/cardrag
COPY --from=build /tmp/cardrag-license-notices /usr/share/licenses/cardrag
COPY --chmod=755 scripts/entrypoint.sh /usr/local/bin/cardrag-entrypoint
COPY --chmod=755 scripts/init-volumes.sh /usr/local/bin/cardrag-volume-init

WORKDIR /app
USER 10001:10001

ENTRYPOINT ["/usr/local/bin/cardrag-entrypoint"]


FROM runtime AS mcp
ENV CARDRAG_CONTAINER_ROLE=mcp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).status==200 else 1)"]
CMD ["cardrag-mcp"]


FROM runtime AS codex_binary
ARG CODEX_VERSION
ARG CODEX_SHA256
ADD --chmod=0644 \
  "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
  /tmp/codex.tar.gz
USER root
RUN apt-get update && \
    apt-get install --yes --no-install-recommends bubblewrap=0.8.0-2+deb12u1 && \
    rm -rf /var/lib/apt/lists/* && \
    printf '%s  %s\n' "${CODEX_SHA256}" /tmp/codex.tar.gz > /tmp/codex.sha256 && \
    sha256sum --check --strict /tmp/codex.sha256 && \
    tar --extract --gzip --file /tmp/codex.tar.gz --directory /usr/local/bin && \
    mv /usr/local/bin/codex-x86_64-unknown-linux-musl /usr/local/bin/codex && \
    chmod 0755 /usr/local/bin/codex && \
    ln -s codex /usr/local/bin/codex-linux-sandbox && \
    install --directory --owner=root --group=root --mode=0755 /etc/codex && \
    rm /tmp/codex.tar.gz /tmp/codex.sha256 && \
    codex --version
COPY --chmod=0444 deploy/codex/config.toml /etc/codex/config.toml
USER 10001:10001


FROM codex_binary AS worker
ENV CARDRAG_CONTAINER_ROLE=worker
USER 10001:10001
CMD ["cardrag-worker"]


FROM runtime AS admin
ENV CARDRAG_CONTAINER_ROLE=admin
USER root
RUN apt-get update && \
    apt-get install --yes --no-install-recommends libpq5=15.19-0+deb12u1 && \
    rm -rf /var/lib/apt/lists/* && \
    install --directory --owner=root --group=root --mode=0755 /usr/local/lib/cardrag-pg17 \
      /usr/share/licenses/cardrag/postgresql-client-17
COPY --from=postgres_client /usr/lib/postgresql/17/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=postgres_client /usr/lib/postgresql/17/bin/pg_restore /usr/local/bin/pg_restore
COPY --from=postgres_client /usr/lib/postgresql/17/bin/psql /usr/local/bin/psql
COPY --from=postgres_client /usr/lib/x86_64-linux-gnu/libpq.so.5.18 \
  /usr/local/lib/cardrag-pg17/libpq.so.5.18
COPY --from=postgres_client /usr/share/doc/postgresql-client-17/copyright \
  /usr/share/licenses/cardrag/postgresql-client-17/COPYRIGHT
ENV LD_LIBRARY_PATH=/usr/local/lib/cardrag-pg17
RUN ln -s libpq.so.5.18 /usr/local/lib/cardrag-pg17/libpq.so.5 && \
    python -c 'import ctypes,subprocess; commands=("pg_dump", "pg_restore", "psql"); assert all(subprocess.check_output((command, "--version"), text=True).split()[2].rstrip(")") == "17.11" for command in commands); libpq=ctypes.CDLL("libpq.so.5"); libpq.PQlibVersion.restype=ctypes.c_int; assert libpq.PQlibVersion() == 180006'
USER 10001:10001
CMD ["cardrag", "--help"]
