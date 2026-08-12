-- Generation-scoped, immutable provenance.  The mutable source_documents
-- catalog is discovery state; these rows are the self-contained facts a
-- sealed generation may expose online.
CREATE TABLE IF NOT EXISTS generation_snapshots (
    generation_id text NOT NULL REFERENCES generations(generation_id) ON DELETE RESTRICT,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    snapshot_id text NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    discovery_mode text NOT NULL CHECK (discovery_mode IN ('current', 'history')),
    completed_at timestamptz NOT NULL,
    PRIMARY KEY (generation_id, issuer),
    UNIQUE (generation_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS generation_documents (
    generation_id text NOT NULL REFERENCES generations(generation_id) ON DELETE RESTRICT,
    document_id text NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    product_code text NOT NULL,
    product_name text NOT NULL,
    document_type text NOT NULL,
    effective_date date NOT NULL,
    source_version text NOT NULL,
    version_sort_key jsonb NOT NULL,
    source_snapshot_id text NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE RESTRICT,
    source_url text NOT NULL,
    discovered_at timestamptz NOT NULL,
    pdf_sha256 text NOT NULL CHECK (pdf_sha256 ~ '^[0-9a-f]{64}$'),
    raw_object_key text NOT NULL,
    pdf_size_bytes bigint NOT NULL CHECK (pdf_size_bytes > 0),
    pdf_page_count integer NOT NULL CHECK (pdf_page_count > 0),
    ocr_sha256 text CHECK (ocr_sha256 IS NULL OR ocr_sha256 ~ '^[0-9a-f]{64}$'),
    ocr_object_key text,
    ocr_pages jsonb,
    ocr_manifest jsonb,
    structured_sha256 text CHECK (
        structured_sha256 IS NULL OR structured_sha256 ~ '^[0-9a-f]{64}$'
    ),
    structured_object_key text,
    structure_schema_version text,
    embedding_provider text,
    embedding_model text,
    embedding_dimension integer CHECK (embedding_dimension IS NULL OR embedding_dimension > 0),
    chunk_policy text,
    chunk_count integer CHECK (chunk_count IS NULL OR chunk_count >= 0),
    embedding_count integer CHECK (embedding_count IS NULL OR embedding_count >= 0),
    index_count integer CHECK (index_count IS NULL OR index_count >= 0),
    is_latest boolean NOT NULL,
    materialized_from_generation_id text REFERENCES generations(generation_id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (generation_id, document_id),
    CHECK (
        (ocr_sha256 IS NULL AND ocr_object_key IS NULL AND ocr_pages IS NULL AND ocr_manifest IS NULL)
        OR
        (ocr_sha256 IS NOT NULL AND ocr_object_key IS NOT NULL
         AND jsonb_typeof(ocr_pages) = 'array' AND jsonb_typeof(ocr_manifest) = 'object')
    ),
    CHECK (
        (structured_sha256 IS NULL AND structured_object_key IS NULL
         AND structure_schema_version IS NULL)
        OR
        (structured_sha256 IS NOT NULL AND structured_object_key IS NOT NULL
         AND structure_schema_version IS NOT NULL)
    ),
    CHECK (
        (embedding_provider IS NULL AND embedding_model IS NULL
         AND embedding_dimension IS NULL AND chunk_policy IS NULL
         AND chunk_count IS NULL AND embedding_count IS NULL AND index_count IS NULL)
        OR
        (embedding_provider IS NOT NULL AND embedding_model IS NOT NULL
         AND embedding_dimension IS NOT NULL AND chunk_policy IS NOT NULL
         AND chunk_count IS NOT NULL AND embedding_count IS NOT NULL AND index_count IS NOT NULL
         AND embedding_count = chunk_count AND index_count = chunk_count)
    )
);
CREATE INDEX IF NOT EXISTS generation_documents_product_idx
    ON generation_documents (
        generation_id, issuer, product_code, is_latest, effective_date DESC, source_version DESC
    );
CREATE INDEX IF NOT EXISTS generation_documents_snapshot_idx
    ON generation_documents (generation_id, source_snapshot_id);

CREATE TABLE IF NOT EXISTS generation_artifacts (
    generation_id text NOT NULL REFERENCES generations(generation_id) ON DELETE RESTRICT,
    manifest_id text NOT NULL,
    artifact_id text NOT NULL,
    document_id text REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    artifact_type text NOT NULL CHECK (artifact_type IN (
        'source_pdf', 'ocr_markdown', 'ocr_page_map', 'structured', 'embedding',
        'lexical_index', 'vector_index', 'generation_manifest', 'quality_report'
    )),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    media_type text NOT NULL,
    manifest_object_key text NOT NULL,
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (generation_id, manifest_id),
    UNIQUE (generation_id, artifact_type, artifact_id)
);
CREATE INDEX IF NOT EXISTS generation_artifacts_document_idx
    ON generation_artifacts (generation_id, document_id, artifact_type);

-- A database-coordinated limiter keeps multiple worker processes polite to
-- each issuer while retaining independent failure domains between issuers.
CREATE TABLE IF NOT EXISTS issuer_rate_limits (
    issuer text PRIMARY KEY CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    next_allowed_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_sealed_generation_child_mutation() RETURNS trigger AS $$
DECLARE
    target_generation_id text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        target_generation_id := NEW.generation_id;
    ELSE
        target_generation_id := OLD.generation_id;
    END IF;
    IF EXISTS (
        SELECT 1 FROM generations g
        WHERE g.generation_id = target_generation_id
          AND g.state IN ('ready', 'published', 'retired')
    ) THEN
        RAISE EXCEPTION 'published or retired generation provenance is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS generation_snapshots_immutable ON generation_snapshots;
CREATE TRIGGER generation_snapshots_immutable
BEFORE INSERT OR UPDATE OR DELETE ON generation_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();

DROP TRIGGER IF EXISTS generation_documents_immutable ON generation_documents;
CREATE TRIGGER generation_documents_immutable
BEFORE INSERT OR UPDATE OR DELETE ON generation_documents
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();

DROP TRIGGER IF EXISTS generation_artifacts_immutable ON generation_artifacts;
CREATE TRIGGER generation_artifacts_immutable
BEFORE INSERT OR UPDATE OR DELETE ON generation_artifacts
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();
