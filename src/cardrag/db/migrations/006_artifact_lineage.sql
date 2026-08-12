-- One immutable byte artifact may be observed through multiple source
-- snapshots or processors.  Keep every canonical manifest/lineage row rather
-- than collapsing them solely by content address.
ALTER TABLE generation_artifacts
    DROP CONSTRAINT IF EXISTS generation_artifacts_generation_id_artifact_type_artifact_id_key;

CREATE INDEX IF NOT EXISTS generation_artifacts_content_idx
    ON generation_artifacts (generation_id, artifact_type, artifact_id);

-- Keep the origin identifier as durable lineage even after the referenced
-- generation ages out under retention. Cross-generation FK retention would
-- otherwise make the configured three-generation policy impossible.
ALTER TABLE generation_documents
    DROP CONSTRAINT IF EXISTS generation_documents_materialized_from_generation_id_fkey;

CREATE OR REPLACE FUNCTION reject_published_generation_mutation() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM generations g
        WHERE g.generation_id = COALESCE(OLD.generation_id, NEW.generation_id)
          AND g.state IN ('ready', 'published', 'retired')
    ) THEN
        RAISE EXCEPTION 'sealed generation evidence is immutable';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_published_evidence_insert() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM generations g
        WHERE g.generation_id = NEW.generation_id
          AND g.state IN ('ready', 'published', 'retired')
    ) THEN
        RAISE EXCEPTION 'sealed generation evidence is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
