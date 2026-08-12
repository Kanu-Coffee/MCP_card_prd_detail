-- Discovery expectations are recorded before download. They let sealing prove
-- that every normalized current source identity reached a content-hashed,
-- fully indexed generation document (rather than disappearing into a failed
-- download job or being mistaken for historical quarantine).
CREATE TABLE IF NOT EXISTS generation_expected_documents (
    generation_id text NOT NULL REFERENCES generations(generation_id) ON DELETE RESTRICT,
    discovery_id text NOT NULL,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    source_snapshot_id text NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    discovery_mode text NOT NULL CHECK (discovery_mode IN ('current', 'history')),
    is_current boolean NOT NULL,
    product_code text NOT NULL,
    document_type text NOT NULL,
    effective_date date NOT NULL,
    source_version text NOT NULL,
    source_url text NOT NULL,
    discovered_at timestamptz NOT NULL,
    PRIMARY KEY (generation_id, discovery_id)
);
CREATE INDEX IF NOT EXISTS generation_expected_current_idx
    ON generation_expected_documents (generation_id, issuer, discovery_mode);

DROP TRIGGER IF EXISTS generation_expected_documents_immutable ON generation_expected_documents;
CREATE TRIGGER generation_expected_documents_immutable
BEFORE INSERT OR UPDATE OR DELETE ON generation_expected_documents
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();
