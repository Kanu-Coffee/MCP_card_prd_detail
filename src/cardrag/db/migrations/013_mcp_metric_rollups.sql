-- Allow the online role to add one anonymous MCP/source observation without
-- granting direct DML on metric_rollups.  The fixed operation/outcome
-- vocabularies and metric name prevent user input, identifiers or credentials
-- from becoming durable dimensions.  Retention deletion remains owner-only.

CREATE OR REPLACE FUNCTION record_mcp_metric_rollup(
    operation_name text,
    outcome_name text,
    duration_seconds double precision
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    safe_operation text;
    safe_outcome text;
    safe_duration double precision;
    rollup_dimensions jsonb;
BEGIN
    safe_operation := CASE
        WHEN operation_name = ANY (ARRAY[
            'search_evidence', 'get_evidence', 'get_product_versions',
            'get_source_pdf', 'get_source_page', 'issuer_catalog',
            'index_status', 'product_resource', 'document_resource',
            'evidence_resource', 'source_ocr_resource',
            'source_ocr_page_resource', 'mcp_transport_auth', 'source_pdf',
            'source_page_png'
        ]::text[]) THEN operation_name
        ELSE 'unknown'
    END;
    safe_outcome := CASE
        WHEN outcome_name = ANY (ARRAY[
            'success', 'no_result', 'degraded', 'denied', 'not_found',
            'timeout', 'error'
        ]::text[]) THEN outcome_name
        ELSE 'error'
    END;
    safe_duration := CASE
        WHEN duration_seconds >= 0 AND duration_seconds <> 'Infinity'::float8
             AND duration_seconds <> '-Infinity'::float8
             AND duration_seconds <> 'NaN'::float8
        THEN least(duration_seconds, 600.0)
        ELSE 0
    END;
    rollup_dimensions := jsonb_build_object(
        'operation', safe_operation,
        'outcome', safe_outcome
    );

    INSERT INTO public.metric_rollups(bucket_start, metric_name, dimensions, count, sum)
    VALUES (
        date_trunc('hour', statement_timestamp()),
        'mcp_operation_duration_seconds',
        rollup_dimensions,
        1,
        safe_duration
    )
    ON CONFLICT (bucket_start, metric_name, dimensions) DO UPDATE SET
        count = public.metric_rollups.count + 1,
        sum = public.metric_rollups.sum + EXCLUDED.sum;
END;
$$;

REVOKE ALL ON FUNCTION record_mcp_metric_rollup(text, text, double precision) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cardrag_mcp') THEN
        REVOKE ALL ON TABLE metric_rollups FROM cardrag_mcp;
        GRANT EXECUTE ON FUNCTION record_mcp_metric_rollup(text, text, double precision)
        TO cardrag_mcp;
    END IF;
END $$;
