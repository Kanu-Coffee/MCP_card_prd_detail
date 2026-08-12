CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id uuid PRIMARY KEY,
    run_type text NOT NULL CHECK (run_type IN ('bulk', 'daily', 'manual')),
    state text NOT NULL CHECK (state IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled')),
    generation_id text,
    scheduled_for timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    pause_requested boolean NOT NULL DEFAULT false,
    cancel_requested boolean NOT NULL DEFAULT false,
    issuer_order text[] NOT NULL DEFAULT ARRAY['woori','kb','shinhan'],
    report jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS run_issuer_status (
    run_id uuid NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    sequence_no integer NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    started_at timestamptz,
    finished_at timestamptz,
    discovered_count integer NOT NULL DEFAULT 0,
    succeeded_count integer NOT NULL DEFAULT 0,
    failed_count integer NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, issuer),
    UNIQUE (run_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS generation_pins (
    generation_id text PRIMARY KEY REFERENCES generations(generation_id) ON DELETE RESTRICT,
    reason text NOT NULL,
    pinned_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS source_snapshots (
    snapshot_id text PRIMARY KEY,
    issuer text NOT NULL CHECK (issuer IN ('woori', 'kb', 'shinhan')),
    discovery_mode text NOT NULL CHECK (discovery_mode IN ('current', 'history')),
    parser_version text NOT NULL,
    source_url text NOT NULL,
    observed_count integer NOT NULL CHECK (observed_count >= 0),
    payload_sha256 text NOT NULL,
    created_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_locks (
    schedule_name text PRIMARY KEY,
    owner_id text NOT NULL,
    lease_until timestamptz NOT NULL,
    fencing_token bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_published_evidence_insert() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM generations g
        WHERE g.generation_id = NEW.generation_id
          AND g.state IN ('published', 'retired')
    ) THEN
        RAISE EXCEPTION 'published generation is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS evidence_immutable_insert ON evidence;
CREATE TRIGGER evidence_immutable_insert BEFORE INSERT ON evidence
FOR EACH ROW EXECUTE FUNCTION reject_published_evidence_insert();
