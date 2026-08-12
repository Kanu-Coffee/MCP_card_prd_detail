-- A child row may never cross generation boundaries.  Checking only OLD on
-- UPDATE allowed a worker to move a mutable row into (or out of) a sealed
-- generation and bypass the immutability decision made by the trigger.
CREATE OR REPLACE FUNCTION reject_sealed_generation_child_mutation() RETURNS trigger AS $$
DECLARE
    target_generation_id text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.generation_id IS DISTINCT FROM OLD.generation_id THEN
        RAISE EXCEPTION 'generation provenance rows cannot change generation_id';
    END IF;
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
        RAISE EXCEPTION 'sealed generation provenance is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_published_generation_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.generation_id IS DISTINCT FROM OLD.generation_id THEN
        RAISE EXCEPTION 'evidence rows cannot change generation_id';
    END IF;
    IF EXISTS (
        SELECT 1 FROM generations g
        WHERE g.generation_id = OLD.generation_id
          AND g.state IN ('ready', 'published', 'retired')
    ) THEN
        RAISE EXCEPTION 'sealed generation evidence is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
