-- pgvector is owned by the PostgreSQL bootstrap superuser because the
-- extension is not trusted. The isolated postgres-extension-upgrade service
-- must therefore complete before the unprivileged CardRAG migrator reaches
-- this release migration.
DO $$
DECLARE
    installed_version text;
BEGIN
    SELECT extversion INTO installed_version
      FROM pg_extension
     WHERE extname = 'vector';
    IF installed_version IS NULL THEN
        RAISE EXCEPTION 'vector extension is not installed';
    END IF;
    IF installed_version <> '0.8.6' THEN
        RAISE EXCEPTION
            'vector extension must be upgraded to 0.8.6 before migration 015 (found %)',
            installed_version;
    END IF;
END $$;

-- Reassert the schema boundary after the privileged extension operation.
-- Runtime roles need type/operator resolution through public, never DDL.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cardrag_worker') THEN
        GRANT USAGE ON SCHEMA public TO cardrag_worker;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cardrag_mcp') THEN
        GRANT USAGE ON SCHEMA public TO cardrag_mcp;
    END IF;
END $$;
