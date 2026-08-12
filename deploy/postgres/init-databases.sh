#!/bin/sh
set -eu

read_secret() {
    secret_path=$1
    if [ ! -r "$secret_path" ]; then
        echo "required secret is not readable: $secret_path" >&2
        exit 1
    fi
    secret_value=$(tr -d '\r\n' < "$secret_path")
    if [ -z "$secret_value" ]; then
        echo "required secret is empty: $secret_path" >&2
        exit 1
    fi
    printf '%s' "$secret_value"
}

app_password=$(read_secret "${CARDRAG_DB_PASSWORD_FILE:-/run/secrets/cardrag_db_password}")
worker_password=$(read_secret "${CARDRAG_WORKER_DB_PASSWORD_FILE:-/run/secrets/cardrag_worker_db_password}")
mcp_password=$(read_secret "${CARDRAG_MCP_DB_PASSWORD_FILE:-/run/secrets/cardrag_mcp_db_password}")
keycloak_password=$(read_secret "${KEYCLOAK_DB_PASSWORD_FILE:-/run/secrets/keycloak_db_password}")
export CARDRAG_INIT_APP_PASSWORD="$app_password"
export CARDRAG_INIT_WORKER_PASSWORD="$worker_password"
export CARDRAG_INIT_MCP_PASSWORD="$mcp_password"
export CARDRAG_INIT_KEYCLOAK_PASSWORD="$keycloak_password"

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_password CARDRAG_INIT_APP_PASSWORD
\getenv worker_password CARDRAG_INIT_WORKER_PASSWORD
\getenv mcp_password CARDRAG_INIT_MCP_PASSWORD
\getenv keycloak_password CARDRAG_INIT_KEYCLOAK_PASSWORD

SELECT format('CREATE ROLE cardrag LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'cardrag')\gexec

SELECT format('CREATE ROLE cardrag_worker LOGIN NOINHERIT PASSWORD %L', :'worker_password')
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'cardrag_worker')\gexec

SELECT format('CREATE ROLE cardrag_mcp LOGIN NOINHERIT PASSWORD %L', :'mcp_password')
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'cardrag_mcp')\gexec

SELECT format('CREATE ROLE keycloak LOGIN PASSWORD %L', :'keycloak_password')
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'keycloak')\gexec

SELECT 'CREATE DATABASE cardrag OWNER cardrag'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cardrag')\gexec

SELECT 'CREATE DATABASE keycloak OWNER keycloak'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec

GRANT CONNECT ON DATABASE cardrag TO cardrag_worker, cardrag_mcp;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname cardrag <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO cardrag;
GRANT USAGE ON SCHEMA public TO cardrag_worker, cardrag_mcp;
SQL

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname keycloak <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO keycloak;
SQL

unset CARDRAG_INIT_APP_PASSWORD CARDRAG_INIT_WORKER_PASSWORD CARDRAG_INIT_MCP_PASSWORD \
    CARDRAG_INIT_KEYCLOAK_PASSWORD
