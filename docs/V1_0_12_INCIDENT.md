# CardRAG v1.0.12 embedding retry incident

## Incident

The v1.0.11 candidate run `1f1763a9cd474a81952a6eb6ffb6e397`
finished with exit code 1 during `corpus-v5 / embedding-v5`. It was not OOM
killed and every v5 capacity/WAL check passed.

The Worker completed 423 embedding batches and cached 26,992 vectors. Four
intermittent provider requests failed after 82, 9, 324, and 8 successful
batches respectively. Each failure consumed one of the four whole-stage
attempts. A stage retry preserved completed cache rows, but repeated the exact
token and cache ledger for 319,133 derived views, adding about 19 minutes of
avoidable work. The final state retained 197,744 unique cache misses.

v1.0.11 intentionally discarded the original `httpx.HTTPError` status and
message, so the retained evidence cannot distinguish 429, 5xx, timeout, or a
transport failure. The fact that the same uncached batch succeeded on the next
stage attempt excludes a deterministic input, token, database, or capacity
failure.

## v1.0.12 correction

v1.0.12 retries an individual OpenRouter embedding request before it can
consume a whole-stage attempt:

- default maximum: 12 wire attempts;
- exponential delay: 1 second up to 60 seconds;
- bounded numeric `Retry-After` support;
- retryable: HTTP 408, 425, 429, all 5xx (including OpenRouter 524 and 529),
  timeout, network, proxy, remote-protocol, decoding failures, and equivalent
  2xx provider error envelopes;
- fail-fast: other 4xx responses, local/unsupported protocol errors, and every
  model, provider, vector, byte-cap, token, or response-contract violation;
- safe diagnostics contain only a reason code, HTTP status category, attempt,
  and delay, never response bodies, URLs, or credentials.

The same policy now covers the OpenRouter endpoint-metadata request used by
preflight. Authentication, billing, authorization, and invalid-request failures
are stored as distinct secret-free categories instead of being mislabeled as a
vector contract violation. `embedding_provider_call_count` is emitted under
the v3 Worker metrics contract and counts actual wire attempts, including
request-local retries, so monitoring and cost estimates no longer undercount.

The provider remains pinned and `allow_fallbacks=false`; v1.0.12 does not mix
DeepInfra and Nebius cache identities. The existing finite stage retry remains
as a final recovery layer only after all request-local transient retries are
exhausted.

The retry controls are:

```text
CARDRAG_EMBEDDING_REQUEST_MAX_ATTEMPTS=12
CARDRAG_EMBEDDING_RETRY_BASE_SECONDS=1
CARDRAG_EMBEDDING_RETRY_CAP_SECONDS=60
```

v1.0.12 is an application reliability patch. It deliberately retains the
`candidate-v1.0.11` data/publication channel and immutable v5 embedding
contract so the verified OCR and embedding cache can be resumed without a
format migration.
