-- Preserve every exact source fragment used to assemble a context chunk.
-- page/span_start/span_end remain a broad navigation envelope only.
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS source_spans jsonb;

-- An envelope cannot be losslessly expanded into the exact constituent fact
-- spans. Never manufacture provenance for an older context chunk: this
-- pre-release schema transition deliberately requires its index generations
-- to be rebuilt from immutable OCR/structure artifacts.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM evidence WHERE source_spans IS NULL) THEN
        RAISE EXCEPTION
            'evidence source span migration requires an offline generation rebuild';
    END IF;
END $$;

ALTER TABLE evidence ALTER COLUMN source_spans SET NOT NULL;
ALTER TABLE evidence DROP CONSTRAINT IF EXISTS evidence_source_spans_nonempty;

CREATE OR REPLACE FUNCTION evidence_source_spans_valid(
    spans jsonb,
    envelope_page_start integer,
    envelope_page_end integer,
    envelope_span_start integer,
    envelope_span_end integer
) RETURNS boolean AS $$
DECLARE
    item jsonb;
    item_page integer;
    item_start integer;
    item_end integer;
    previous_page integer := NULL;
    previous_end integer := NULL;
    minimum_page integer := NULL;
    maximum_page integer := NULL;
    minimum_start integer := NULL;
    maximum_end integer := NULL;
BEGIN
    IF jsonb_typeof(spans) <> 'array' OR jsonb_array_length(spans) = 0 THEN
        RETURN false;
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(spans) LOOP
        IF jsonb_typeof(item) <> 'object'
           OR NOT (item ?& ARRAY['page','start','end','quote_sha256'])
           OR (item->>'quote_sha256') !~ '^[0-9a-f]{64}$' THEN
            RETURN false;
        END IF;
        BEGIN
            item_page := (item->>'page')::integer;
            item_start := (item->>'start')::integer;
            item_end := (item->>'end')::integer;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN false;
        END;
        IF item_page < 1 OR item_start < 0 OR item_end <= item_start THEN
            RETURN false;
        END IF;
        IF previous_page IS NOT NULL AND (
            item_page < previous_page
            OR (item_page = previous_page AND item_start < previous_end)
        ) THEN
            RETURN false;
        END IF;
        previous_page := item_page;
        previous_end := item_end;
        minimum_page := LEAST(COALESCE(minimum_page, item_page), item_page);
        maximum_page := GREATEST(COALESCE(maximum_page, item_page), item_page);
        minimum_start := LEAST(COALESCE(minimum_start, item_start), item_start);
        maximum_end := GREATEST(COALESCE(maximum_end, item_end), item_end);
    END LOOP;
    RETURN minimum_page=envelope_page_start AND maximum_page=envelope_page_end
       AND minimum_start=envelope_span_start AND maximum_end=envelope_span_end;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

ALTER TABLE evidence ADD CONSTRAINT evidence_source_spans_nonempty CHECK (
    evidence_source_spans_valid(source_spans, page_start, page_end, span_start, span_end)
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_worker') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE evidence TO cardrag_worker;
    END IF;
END $$;
