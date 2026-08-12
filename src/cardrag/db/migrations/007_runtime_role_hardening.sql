-- Narrow runtime roles after every v1 table exists.  Migration/admin work is
-- intentionally retained by the owner role only.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_worker') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM cardrag_worker;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM cardrag_worker;

        GRANT SELECT, INSERT, UPDATE ON TABLE jobs, job_attempts TO cardrag_worker;
        GRANT SELECT, INSERT ON TABLE stage_checkpoints TO cardrag_worker;
        GRANT SELECT ON TABLE pipeline_runs TO cardrag_worker;

        GRANT SELECT, INSERT ON TABLE source_snapshots TO cardrag_worker;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE source_documents TO cardrag_worker;
        GRANT SELECT ON TABLE generations, active_generation TO cardrag_worker;
        GRANT SELECT, INSERT, UPDATE ON TABLE
            generation_snapshots, generation_documents
        TO cardrag_worker;
        GRANT SELECT, INSERT, DELETE ON TABLE generation_artifacts TO cardrag_worker;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE evidence TO cardrag_worker;
        GRANT SELECT, INSERT, UPDATE ON TABLE issuer_rate_limits TO cardrag_worker;

        GRANT SELECT, INSERT, UPDATE ON TABLE metric_rollups TO cardrag_worker;
        -- Audit and retention deletion are owner-only admin operations.

        GRANT USAGE, SELECT ON SEQUENCE job_attempts_id_seq TO cardrag_worker;

        ALTER DEFAULT PRIVILEGES FOR ROLE cardrag IN SCHEMA public
            REVOKE ALL ON TABLES FROM cardrag_worker;
        ALTER DEFAULT PRIVILEGES FOR ROLE cardrag IN SCHEMA public
            REVOKE ALL ON SEQUENCES FROM cardrag_worker;
    END IF;
END $$;
