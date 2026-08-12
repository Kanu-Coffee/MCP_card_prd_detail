# CardRAG observability runbook

This runbook covers the initial diagnostic alerts. Thresholds are not contractual SLOs; revise them only
after the Shinhan BULK and representative query benchmarks. Result quality remains the primary performance
criterion, while the finite request timeout remains the failure-isolation boundary.

## Safe access and data policy

- `/metrics` accepts only the actual loopback socket peer. Do not publish or proxy this path through Nginx
  Proxy Manager. Scrape it from the MCP container's network namespace or a loopback-local collector.
- Prometheus labels contain only fixed route templates, HTTP method/status class, supported issuer, pipeline
  stage and lifecycle state. Search text, HTTP bodies, Authorization headers, tokens, document IDs, provider
  responses and signed URLs are never labels.
- JSON application events use `request_id`, `run_id`, `job_id` and `generation_id` for correlation. Document
  IDs are represented only by `document_id_hash`. Search by those fields; do not paste secrets or source text
  into an incident ticket.
- The owner-only `cardrag-retention.timer` runs the admin `cardrag retention prune` one-shot at 04:00 KST;
  it retains the latest three successful generations plus active/pinned generations, removes failed
  generations after seven days, audit metadata after 90 days, and metric rollups after one year. The worker
  cannot perform these deletes. The MCP lifecycle removes expired page PNG cache entries.
  Docker/collector log retention and viewer RBAC are host responsibilities; keep application logs no longer
  than the approved operational window.

Safe local probe:

```sh
docker compose exec --no-TTY mcp python -c \
  'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=3).status)'
```

Never add an access token to this command or to a URL.

## DailyOneShotInterrupted

If `cardrag-daily.service` exits after durable run creation, do not launch a new
daily run. Use the owner/admin container to discover and resume the same run ID:

```sh
docker compose --profile ops run --rm --no-deps admin cardrag run list --state running
docker compose --profile ops run --rm --no-deps admin cardrag run status RUN_ID
docker compose --profile ops run --rm --no-deps admin cardrag run finalize RUN_ID
```

Confirm the list contains the expected daily run and correlate its creation time
with the systemd journal. `finalize` reuses durable idempotency keys, continues
missing issuers in order, and applies the ordinary seal/publish gates. If more
than one unexplained running daily run appears, preserve state and investigate
the scheduler lease before finalizing either one.

## CardRAGQueueOlderThanDailyCycle

1. Inspect queue depth and oldest age by `issuer`, `stage` and `state`.
2. Correlate recent `worker.job.completed` events by run/job ID and stable `error_code`.
3. Confirm worker health, provider quota and disk capacity without printing job payloads.
4. Pause the affected issuer run if retries are amplifying load; redrive only after the stable error is fixed.

## CardRAGDeadLetterCreated

1. Locate the durable dead-letter row by operator CLI and its non-sensitive `last_error_code`.
2. Verify whether the failure is permanent (invalid PDF/schema) or an exhausted transient condition.
3. Preserve source/generation artifacts for diagnosis. Redrive explicitly; never edit attempt/fencing state.

## CardRAGRetryBurst

1. Group structured completion events by issuer, stage and error code.
2. Check issuer availability, HTTP rate limits, Codex/OpenRouter authorization and quota separately.
3. Allow bounded backoff to operate. Do not shorten retry delays during a provider outage.

## CardRAGLeaseReclaimBurst

1. Check worker termination/OOM events and compare stage duration with the configured lease heartbeat.
2. Verify PostgreSQL latency and host clock health.
3. Do not accept a stale worker's completion; fencing-token rejection is expected after lease loss.

## CardRAGQueueEtaExceedsOneDay

ETA is a local exponential moving estimate and starts at zero before the first completed sample. Compare it
with actual completion throughput, queue age and provider quotas. Scale worker concurrency only within provider
limits and host memory; avoid parallelizing OCR until quality and restart behavior remain stable.

## CardRAGMCPServerErrorRatio

1. Check `/health/ready`, active generation availability, database reachability and embedding provider status.
2. Correlate a failing response's `X-Request-ID` with `http.request.failed`. The log intentionally contains
   only an exception class, never the query or token.
3. Roll back to the prior validated generation if the active generation is the cause.

## CardRAGMCPDiagnosticLatencyHigh

The 30-second value is the ADR-0005 initial warning threshold, not a promised P95. Compare latency with timeout rate,
result quality, concurrent request count and provider latency. Prefer narrowing excessive candidate work or
restoring provider health over weakening evidence-quality gates.

## CardRAGMCPNoResultRatioHigh

This warning requires at least 20 searches in 30 minutes, so low-traffic periods do not trigger on a single
specialized query. Check the active generation's latest-document coverage, issuer filters and retrieval gold
set. The metric contains only an outcome count; retrieve a representative query from an approved test set
instead of adding production query text to logs, metrics or incident tickets.
