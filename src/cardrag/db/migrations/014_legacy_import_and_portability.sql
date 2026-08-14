-- Portable legacy adoption and resumable import ledger.
ALTER TABLE pipeline_runs DROP CONSTRAINT IF EXISTS pipeline_runs_run_type_check;
ALTER TABLE pipeline_runs ADD CONSTRAINT pipeline_runs_run_type_check
    CHECK (run_type IN ('bulk', 'daily', 'manual', 'legacy_import'));

CREATE TABLE IF NOT EXISTS legacy_imports (
    import_id uuid PRIMARY KEY,
    bundle_id text NOT NULL,
    bundle_sha256 text NOT NULL CHECK (bundle_sha256 ~ '^[0-9a-f]{64}$'),
    run_id uuid NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
    generation_id text NOT NULL REFERENCES generations(generation_id) ON DELETE RESTRICT,
    state text NOT NULL CHECK (state IN (
        'preparing', 'processing', 'ready_to_finalize', 'finalizing',
        'succeeded', 'failed', 'cancelled'
    )),
    phase text NOT NULL,
    attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
    no_publish boolean NOT NULL DEFAULT true,
    report jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(report) = 'object'),
    last_error_code text,
    started_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (run_id),
    UNIQUE (generation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS legacy_imports_one_active_bundle_idx
    ON legacy_imports(bundle_sha256)
    WHERE state IN ('preparing', 'processing', 'ready_to_finalize', 'finalizing');

CREATE TABLE IF NOT EXISTS legacy_import_documents (
    import_id uuid NOT NULL REFERENCES legacy_imports(import_id) ON DELETE CASCADE,
    document_id text NOT NULL,
    document_key text NOT NULL,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    pdf_sha256 text NOT NULL CHECK (pdf_sha256 ~ '^[0-9a-f]{64}$'),
    ocr_sha256 text CHECK (ocr_sha256 IS NULL OR ocr_sha256 ~ '^[0-9a-f]{64}$'),
    disposition text NOT NULL CHECK (disposition IN ('adopted', 'reocr', 'quarantined')),
    state text NOT NULL CHECK (state IN (
        'pending', 'seeded', 'queued', 'processing', 'succeeded', 'failed'
    )),
    job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    error_code text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (import_id, document_id),
    UNIQUE (import_id, document_key)
);
CREATE INDEX IF NOT EXISTS legacy_import_documents_progress_idx
    ON legacy_import_documents(import_id, state, disposition);

-- Portable generation roots are relative to the configured generation store.
-- Keep the legacy column for schema compatibility, but canonicalize its value
-- to the same relative key so a logical dump never retains an old host path.
ALTER TABLE generations ADD COLUMN IF NOT EXISTS root_key text;
UPDATE generations SET root_key = 'generations/' || generation_id WHERE root_key IS NULL;
UPDATE generations SET root_uri = 'generations/' || generation_id;
ALTER TABLE generations ALTER COLUMN root_key SET NOT NULL;
ALTER TABLE generations DROP CONSTRAINT IF EXISTS generations_root_key_portable_check;
ALTER TABLE generations ADD CONSTRAINT generations_root_key_portable_check CHECK (
    root_key = 'generations/' || generation_id
    AND root_key !~ '^/'
    AND root_key !~ '(^|/)\.\.(/|$)'
);
CREATE UNIQUE INDEX IF NOT EXISTS generations_root_key_idx ON generations(root_key);
ALTER TABLE generations DROP CONSTRAINT IF EXISTS generations_root_uri_portable_check;
ALTER TABLE generations ADD CONSTRAINT generations_root_uri_portable_check CHECK (
    root_uri = root_key
);

CREATE OR REPLACE FUNCTION set_generation_root_key() RETURNS trigger AS $$
BEGIN
    NEW.root_key := 'generations/' || NEW.generation_id;
    NEW.root_uri := NEW.root_key;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS generations_set_root_key ON generations;
CREATE TRIGGER generations_set_root_key
BEFORE INSERT OR UPDATE OF generation_id, root_uri, root_key ON generations
FOR EACH ROW EXECUTE FUNCTION set_generation_root_key();
REVOKE ALL ON FUNCTION set_generation_root_key() FROM PUBLIC;

-- One centrally versioned compatibility predicate is shared by discovery,
-- materialization and generation validation. Legacy provenance is explicit;
-- it is never represented as a current Codex/PDFium attempt.
CREATE OR REPLACE FUNCTION cardrag_ocr_manifest_reusable(
    manifest jsonb,
    pdf_sha256 text,
    ocr_sha256 text,
    prompt_version text,
    renderer text,
    reasoning_effort text,
    render_scale double precision,
    chunk_pages integer,
    codex_model text,
    fallback_model text
) RETURNS boolean AS $$
    SELECT COALESCE(
        CASE
            WHEN manifest->>'schema_version' = 'cardrag.legacy-ocr-adoption.v1' THEN
                manifest->>'adoption_policy' = 'cardrag.legacy-ocr-adoption.v1'
                AND manifest->>'status' = 'adopted'
                AND manifest->>'pdf_sha256' = pdf_sha256
                AND manifest->>'ocr_sha256' = ocr_sha256
            ELSE
                manifest->'attempt'->>'prompt_version' = prompt_version
                AND manifest->'attempt'->>'renderer' = renderer
                AND (
                    manifest->'attempt'->>'provider' <> 'codex-exec'
                    OR manifest->'attempt'->>'reasoning_effort' = reasoning_effort
                )
                AND CASE
                    WHEN manifest->'attempt'->>'render_scale' ~ '^[0-9]+(\.[0-9]+)?$'
                    THEN (manifest->'attempt'->>'render_scale')::double precision = render_scale
                    ELSE false
                END
                AND CASE
                    WHEN manifest->'attempt'->>'chunk_pages' ~ '^[0-9]+$'
                    THEN (manifest->'attempt'->>'chunk_pages')::integer = chunk_pages
                    ELSE false
                END
                AND (
                    (manifest->'attempt'->>'provider' = 'codex-exec'
                     AND manifest->'attempt'->>'model' = codex_model)
                    OR
                    (manifest->'attempt'->>'provider' = 'openrouter'
                     AND manifest->'attempt'->>'model' = fallback_model)
                )
        END,
        false
    )
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE;
REVOKE ALL ON FUNCTION cardrag_ocr_manifest_reusable(
    jsonb, text, text, text, text, text, double precision, integer, text, text
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION cardrag_legacy_adoption_bound(
    manifest jsonb,
    document_id text,
    pdf_sha256 text,
    ocr_sha256 text,
    allowed_import_states text[]
) RETURNS boolean AS $$
    SELECT COALESCE(EXISTS (
        SELECT 1
        FROM legacy_imports i
        JOIN legacy_import_documents d USING (import_id)
        WHERE i.import_id::text = manifest->>'import_id'
          AND i.bundle_id = manifest->>'bundle_id'
          AND i.bundle_sha256 = manifest->>'bundle_sha256'
          AND i.state = ANY($5)
          AND d.document_id = $2
          AND d.disposition = 'adopted'
          AND d.pdf_sha256 = $3
          AND d.ocr_sha256 = $4
          AND manifest->>'pdf_sha256' = d.pdf_sha256
          AND manifest->>'ocr_sha256' = d.ocr_sha256
    ), false)
$$ LANGUAGE sql STABLE PARALLEL SAFE;
REVOKE ALL ON FUNCTION cardrag_legacy_adoption_bound(
    jsonb, text, text, text, text[]
) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_worker') THEN
        GRANT SELECT ON TABLE legacy_import_documents, legacy_imports TO cardrag_worker;
        GRANT EXECUTE ON FUNCTION cardrag_ocr_manifest_reusable(
            jsonb, text, text, text, text, text, double precision, integer, text, text
        ) TO cardrag_worker;
        GRANT EXECUTE ON FUNCTION cardrag_legacy_adoption_bound(
            jsonb, text, text, text, text[]
        ) TO cardrag_worker;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_runtime') THEN
        GRANT EXECUTE ON FUNCTION cardrag_ocr_manifest_reusable(
            jsonb, text, text, text, text, text, double precision, integer, text, text
        ) TO cardrag_runtime;
        GRANT EXECUTE ON FUNCTION cardrag_legacy_adoption_bound(
            jsonb, text, text, text, text[]
        ) TO cardrag_runtime;
    END IF;
END $$;
