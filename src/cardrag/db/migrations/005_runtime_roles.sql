-- Runtime least privilege.  The database owner ``cardrag`` runs migrations
-- and operator commands.  Worker and MCP credentials are distinct LOGIN roles
-- created by deploy/postgres/init-databases.sh before this migration runs.

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;

DO $$
BEGIN
    -- Plain developer/test PostgreSQL instances intentionally do not create
    -- deployment roles. Production init creates both before migrations.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_worker')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_mcp') THEN
        GRANT USAGE ON SCHEMA public TO cardrag_worker, cardrag_mcp;

        -- The offline worker owns durable pipeline state transitions, catalog
        -- build, metrics rollups and retention, but no schema DDL.
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO cardrag_worker;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cardrag_worker;

        -- Online MCP is read-only except for append-only access auditing.
        GRANT SELECT ON TABLE
            active_generation, generations, generation_documents, evidence
        TO cardrag_mcp;
        GRANT INSERT ON TABLE audit_events TO cardrag_mcp;
        GRANT USAGE, SELECT ON SEQUENCE audit_events_id_seq TO cardrag_mcp;
    END IF;

    -- Default privileges can name an owner only where the deployment owner
    -- exists; the normal migration session already owns test objects.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_worker') THEN
        ALTER DEFAULT PRIVILEGES FOR ROLE cardrag IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cardrag_worker;
        ALTER DEFAULT PRIVILEGES FOR ROLE cardrag IN SCHEMA public
            GRANT USAGE, SELECT ON SEQUENCES TO cardrag_worker;
    END IF;
END $$;
