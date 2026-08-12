-- Artifact bytes are content addressed and may legitimately be referenced by
-- more than one document in the same generation. Migration 006 intended to
-- remove this constraint, but PostgreSQL's generated 63-byte identifier is
-- shorter than the explicit name used there, so the constraint survived.
ALTER TABLE generation_artifacts
    DROP CONSTRAINT IF EXISTS generation_artifacts_generation_id_artifact_type_artifact_i_key;
