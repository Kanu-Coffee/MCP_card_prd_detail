-- Preserve source-site version labels while treating changed PDF bytes as a
-- distinct immutable document version.  The discovery ID exists before the
-- bytes are downloaded; the final document ID also includes pdf_sha256.
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS discovery_id text;
UPDATE source_documents SET discovery_id = document_id WHERE discovery_id IS NULL;
ALTER TABLE source_documents ALTER COLUMN discovery_id SET NOT NULL;

DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'source_documents'::regclass AND contype = 'u'
    LIMIT 1;
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE source_documents DROP CONSTRAINT %I', constraint_name);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS source_documents_discovery_idx
    ON source_documents (discovery_id, last_seen_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS source_documents_content_version_idx
    ON source_documents (
        issuer, product_code, document_type, effective_date, source_version, pdf_sha256
    )
    WHERE pdf_sha256 IS NOT NULL;
