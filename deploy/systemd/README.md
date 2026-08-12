# Daily pipeline and retention systemd timer hand-off

The scheduler is deliberately a host timer plus a one-shot Compose admin job,
not another long-running scheduler inside Compose. `Persistent=true` catches up
after host downtime, and systemd will not start a second copy while the prior
one-shot is still active. PostgreSQL remains the authoritative run/job state.

Before installing the units:

1. Place the reviewed checkout at `/opt/cardrag`, or override
   `WorkingDirectory` and `Documentation` with a systemd drop-in.
2. Create a dedicated `cardrag` host account. Membership in the `docker` group
   is root-equivalent and must be limited to approved operators.
3. Put only non-secret Compose selectors in `/etc/cardrag/cardrag.env`, for
   example `CARDRAG_SECRETS_DIR=/etc/cardrag/secrets`. Keep that directory mode
   `0700`, follow `deploy/secrets/README.md` for Compose-compatible file modes,
   and keep it outside the checkout.
4. Start and verify `postgres`, `migrate`, `worker`, and the last published
   generation before enabling the timer. The one-shot uses `--no-deps` so a
   schedule event cannot silently start or rebuild infrastructure.
5. Confirm `cardrag run daily` rejects a concurrent daily run through the
   PostgreSQL scheduler lock before operational enablement.

Install and validate without executing the job:

```bash
sudo install -o root -g root -m 0644 \
  deploy/systemd/cardrag-daily.service \
  deploy/systemd/cardrag-daily.timer \
  deploy/systemd/cardrag-retention.service \
  deploy/systemd/cardrag-retention.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/cardrag-daily.service \
  /etc/systemd/system/cardrag-daily.timer \
  /etc/systemd/system/cardrag-retention.service \
  /etc/systemd/system/cardrag-retention.timer
systemctl list-timers cardrag-daily.timer cardrag-retention.timer
```

After reviewing the resolved user, paths, environment, and timer, enable it:

```bash
sudo systemctl enable --now cardrag-daily.timer cardrag-retention.timer
```

The service waits for the three issuer job graphs and their required ten-minute
gaps, so a successful exit may take hours. Inspect non-secret state with
`docker compose --profile ops run --rm --no-deps admin cardrag job status` and
`journalctl -u cardrag-daily.service`; never copy provider output or tokens into
the journal. A manual catch-up is `sudo systemctl start cardrag-daily.service`.

If the one-shot supervisor exits after it creates a run, do not start a second
daily run. Discover the durable running ID from PostgreSQL, inspect it, and
resume supervision/finalization with that same ID:

```bash
docker compose --profile ops run --rm --no-deps admin \
  cardrag run list --state running
docker compose --profile ops run --rm --no-deps admin \
  cardrag run status RUN_ID
docker compose --profile ops run --rm --no-deps admin \
  cardrag run finalize RUN_ID
```

`finalize` is idempotent for already-enqueued issuer work, resumes a missing
issuer in the original order, waits for terminal jobs, and publishes only after
the normal quality gates. Record the returned state before re-enabling a manual
catch-up.

At 04:00 KST the separate retention one-shot runs through the admin image and
owner DSN. The single `cardrag retention prune` command removes all but the
latest three successful generations unless pinned or active, removes failed
generations only after seven days, removes audit rows older than 90 days, and
removes anonymous metric rollups older than one year. Generation deletion is
serialized with publication and updates PostgreSQL before pruning the matching
generation/build trees; a failed one-shot is safe to retry. The continuously
running worker credential cannot perform these owner-only deletes. Inspect its bounded result with
`journalctl -u cardrag-retention.service`; trigger a reviewed manual run with
`sudo systemctl start cardrag-retention.service`.
