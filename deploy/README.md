# Deployment support boundary

The supported v1 deployment surface is intentionally limited to:

- `worker/`: one-shot Worker Compose project and the 03:00 Asia/Seoul timer;
- `mcp/`: always-on MCP Compose project.

`postgres/`, `keycloak/`, `portainer/`, the old `systemd/` units, and the monitoring
rules are v0.2.1 cutover material only. They are not referenced by the root Compose,
the v1 Dockerfile, CI image builds, or the v1 release workflow. Keep an existing v0.2.1
deployment read-only for seven successful shadow runs, then remove it from the host.
The new runtime never starts or connects to those services.

## Supported entry points

Copy `simple.env.example` to two root-owned files and keep only the variables
needed by each role:

```text
/etc/cardrag/worker.env
/etc/cardrag/mcp.env
```

The `*_SECRET_FILE` values are host paths consumed by Docker Compose. The
corresponding `*_FILE` values inside the containers are supplied by
`compose.secrets.yaml`; do not put both a direct secret and its file variant in
the same environment. The Worker and MCP intentionally use the same WebDAV
credential. The Bearer token must contain at least 32 non-whitespace
characters.

Run the Worker once and start MCP continuously with:

```bash
docker compose --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run --rm worker run

docker compose --env-file /etc/cardrag/mcp.env \
  -f deploy/mcp/compose.yaml \
  -f deploy/mcp/compose.secrets.yaml \
  up -d --wait
```

Add the role's `compose.ca.yaml` as a final overlay only when a private WebDAV
CA is configured. MCP is published on `127.0.0.1:8000` by default; its internal
`0.0.0.0` listener is not directly exposed beyond the Docker host.

The canonical preflight, migration, timer installation, recovery, GC, shadow,
and rollback procedure is [`../docs/SIMPLE_RUNTIME.md`](../docs/SIMPLE_RUNTIME.md).
