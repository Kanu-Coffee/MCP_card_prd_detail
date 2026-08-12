CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    name text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    CREATE TYPE job_state AS ENUM (
        'queued', 'running', 'retry_wait', 'succeeded', 'dead_letter', 'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE generation_state AS ENUM (
        'building', 'validating', 'ready', 'published', 'failed', 'retired'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    stage text NOT NULL,
    document_id text,
    idempotency_key text NOT NULL,
    state job_state NOT NULL DEFAULT 'queued',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_until timestamptz,
    fencing_token bigint NOT NULL DEFAULT 0,
    last_error_code text,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (state, available_at, created_at);
CREATE INDEX IF NOT EXISTS jobs_lease_idx ON jobs (state, lease_until);

CREATE TABLE IF NOT EXISTS job_attempts (
    id bigserial PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_no integer NOT NULL,
    fencing_token bigint NOT NULL,
    worker_id text NOT NULL,
    provider text,
    model text,
    config_hash text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    outcome text,
    error_code text,
    UNIQUE (job_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS stage_checkpoints (
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_no integer NOT NULL,
    unit_key text NOT NULL,
    input_hash text NOT NULL,
    output_hash text NOT NULL,
    artifact_uri text NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, attempt_no, unit_key)
);

CREATE TABLE IF NOT EXISTS source_documents (
    document_id text PRIMARY KEY,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    product_code text NOT NULL,
    product_name text NOT NULL,
    document_type text NOT NULL,
    effective_date date NOT NULL,
    source_version text NOT NULL,
    version_sort_key jsonb NOT NULL,
    source_snapshot_id text NOT NULL,
    source_url text,
    pdf_sha256 text,
    raw_object_key text,
    last_seen_at timestamptz NOT NULL,
    tombstoned_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (issuer, product_code, document_type, effective_date, source_version)
);
CREATE INDEX IF NOT EXISTS source_documents_product_idx
    ON source_documents (issuer, product_code, effective_date DESC);

CREATE TABLE IF NOT EXISTS generations (
    generation_id text PRIMARY KEY,
    state generation_state NOT NULL,
    manifest_sha256 text NOT NULL,
    root_uri text NOT NULL,
    schema_version text NOT NULL,
    embedding_provider text NOT NULL,
    embedding_model text NOT NULL,
    embedding_dimension integer NOT NULL CHECK (embedding_dimension > 0),
    latest_document_count integer NOT NULL DEFAULT 0,
    latest_covered_count integer NOT NULL DEFAULT 0,
    historical_quarantine_count integer NOT NULL DEFAULT 0,
    pinned boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    retired_at timestamptz,
    CHECK (latest_covered_count <= latest_document_count)
);

CREATE TABLE IF NOT EXISTS active_generation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    generation_id text NOT NULL REFERENCES generations(generation_id),
    fencing_token bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidence (
    generation_id text NOT NULL REFERENCES generations(generation_id) ON DELETE RESTRICT,
    evidence_id text NOT NULL,
    document_id text NOT NULL,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    product_code text NOT NULL,
    product_name text NOT NULL,
    document_type text NOT NULL,
    effective_date date NOT NULL,
    source_version text NOT NULL,
    section_type text NOT NULL,
    page_start integer NOT NULL CHECK (page_start > 0),
    page_end integer NOT NULL CHECK (page_end >= page_start),
    span_start integer NOT NULL CHECK (span_start >= 0),
    span_end integer NOT NULL CHECK (span_end >= span_start),
    text text NOT NULL,
    text_sha256 text NOT NULL,
    confidence real NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    is_latest boolean NOT NULL,
    search_tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(product_name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(section_type, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(text, '')), 'C')
    ) STORED,
    embedding vector(1536),
    PRIMARY KEY (generation_id, evidence_id),
    UNIQUE (generation_id, document_id, page_start, span_start, span_end, text_sha256)
);
CREATE INDEX IF NOT EXISTS evidence_filter_idx
    ON evidence (generation_id, issuer, product_code, is_latest, effective_date DESC, section_type);
CREATE INDEX IF NOT EXISTS evidence_fts_idx ON evidence USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS evidence_vector_idx ON evidence
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 96);

CREATE TABLE IF NOT EXISTS audit_events (
    id bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    event_type text NOT NULL,
    request_id text NOT NULL,
    subject_hash text NOT NULL,
    client_id text,
    scopes text[] NOT NULL DEFAULT '{}',
    document_id text,
    outcome text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS audit_events_retention_idx ON audit_events (occurred_at);

CREATE TABLE IF NOT EXISTS metric_rollups (
    bucket_start timestamptz NOT NULL,
    metric_name text NOT NULL,
    dimensions jsonb NOT NULL DEFAULT '{}'::jsonb,
    count bigint NOT NULL DEFAULT 0,
    sum double precision NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_start, metric_name, dimensions)
);

CREATE OR REPLACE FUNCTION reject_published_generation_mutation() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM generations g
        WHERE g.generation_id = COALESCE(OLD.generation_id, NEW.generation_id)
          AND g.state IN ('published', 'retired')
    ) THEN
        RAISE EXCEPTION 'published generation is immutable';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS evidence_immutable_update ON evidence;
CREATE TRIGGER evidence_immutable_update BEFORE UPDATE OR DELETE ON evidence
FOR EACH ROW EXECUTE FUNCTION reject_published_generation_mutation();
