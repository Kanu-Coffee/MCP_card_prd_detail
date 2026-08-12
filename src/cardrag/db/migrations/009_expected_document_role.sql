DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_worker') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE generation_expected_documents TO cardrag_worker;
    END IF;
END $$;
