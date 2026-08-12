# Runtime secret files

Create a dedicated secret directory with mode `0700`; do not commit it.
Docker Compose file-backed secrets preserve host ownership/mode instead of
remapping to each container UID. Because PostgreSQL, Keycloak, and CardRAG use
different non-root UIDs (and one DB secret is shared at initialization), set
the files themselves to mode `0444` inside that protected directory. Compose
mounts only the named secrets into each service and all mounts are read-only.
If the deployment uses Swarm or another secret store that can set per-service
ownership, use that stronger facility instead.

```text
postgres_admin_password.txt
cardrag_db_password.txt
cardrag_worker_db_password.txt
cardrag_mcp_db_password.txt
cardrag_database_url.txt
cardrag_worker_database_url.txt
cardrag_mcp_database_url.txt
keycloak_db_password.txt
keycloak_admin_password.txt
openrouter_api_key.txt
```

`cardrag_database_url.txt` is the owner/admin DSN and contains the password from
`cardrag_db_password.txt`, for example
`postgresql://cardrag:REPLACE_WITH_URL_ENCODED_PASSWORD@postgres:5432/cardrag`.
The password must be URL-encoded when it contains reserved URI characters.

Use separate values for the two runtime identities:

```text
cardrag_worker_database_url.txt = postgresql://cardrag_worker:...@postgres:5432/cardrag
cardrag_mcp_database_url.txt    = postgresql://cardrag_mcp:...@postgres:5432/cardrag
```

The migration/admin container keeps the owner DSN. The worker role has row-level
DML but no DDL or grant authority. The MCP role has generation/catalog read and
append-only `audit_events` access. It may record only allow-listed anonymous
operation/outcome rollups through `record_mcp_metric_rollup`; direct
`metric_rollups` table DML remains revoked. It cannot change jobs, evidence,
generation pointers, or schema objects. Never reuse one password across these
files.

Use the bootstrap secret only for the first Keycloak start:

```bash
docker compose \
  -f compose.yaml \
  -f deploy/keycloak/bootstrap.compose.yaml \
  up -d postgres keycloak
```

Create a named permanent administrator, verify that account, and rotate or
revoke the bootstrap administrator. Then recreate Keycloak with only the base
`compose.yaml` and securely remove `keycloak_admin_password.txt`. The base
Compose definition does not mount that one-time secret, so later restarts
cannot silently reuse it.

The default local issuer is
`http://cardrag-keycloak.localhost:8080/realms/cardrag`. For an HTTPS deployment,
set `KEYCLOAK_PUBLIC_URL` to the Keycloak origin and `CARDRAG_OIDC_ISSUER` to the
same origin plus `/realms/cardrag`; an issuer mismatch invalidates every token.

Create every human or service client manually. Do not copy a client secret into
the realm JSON. Assign `search` and `source_pdf` only as optional client scopes
that the approved client actually needs; the imported `cardrag-mcp` bearer-only
client is the resource audience, not an interactive login client. Use
Authorization Code with PKCE for public human clients and Client Credentials
for confidential service clients. Validate the resulting access token has
`iss` equal to `CARDRAG_OIDC_ISSUER`, `aud` containing `cardrag-mcp`, and the
requested scope before connecting it to MCP.
