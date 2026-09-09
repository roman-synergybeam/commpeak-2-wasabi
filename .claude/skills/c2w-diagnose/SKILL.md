---
name: c2w-diagnose
description: Triage failed or stalled recording transfers by error class. Use when transfers are failing, the queue is stuck, recordings sit in TRANSFERRING, verification mismatches appear, or an alert fires for a connection.
---

# Diagnosing transfer failures

## Start with the distribution, not an individual row

The error class tells you which of a handful of distinct problems you have.

```sql
SELECT error_class, count(*), max(updated_at) AS latest
FROM transfer_jobs
WHERE state = 'FAILED'
GROUP BY error_class ORDER BY 2 DESC;
```

| Class | Meaning | Action |
|---|---|---|
| `ACL_ERROR` | IP not whitelisted at CommPeak (usual cause), or bucket policy | Add this server's public IP to the account ACL, then requeue |
| `AUTH_ERROR` | token/secret wrong or rotated | Re-enter credentials via `c2w-connection`; do not retry first |
| `CONFIG_ERROR` | bad endpoint/region/bucket, or clock skew | Check endpoint and `timedatectl`; `RequestTimeTooSkewed` means NTP |
| `NOT_FOUND` | object vanished at source before we copied it | Expected during retention churn; state becomes `MISSING_SOURCE` |
| `RATE_LIMIT` | throttled | `c2w-admin settings set source.concurrency_per_connection 5`; the ladder already backs off |
| `NETWORK_ERROR` | timeouts, dropped uplink | Transient; check sustained throughput and MTU |
| `CHECKSUM_ERROR` | source and destination bytes disagree | Re-transferred once automatically; repeated means a real corruption path — investigate before requeueing |
| `STORAGE_ERROR` | destination rejected the write | Wasabi quota, bucket policy, or object-lock |

Note that `AUTH_ERROR`, `ACL_ERROR`, `CONFIG_ERROR` and `PERMISSION_ERROR` are
deliberately **not** retryable — they alert immediately instead. If you see them
accumulating without an alert having fired, the alerting path is broken too.

## Stalled, not failed

Jobs in `RUNNING` past their lease mean a worker died mid-transfer:

```sql
SELECT id, recording_id, claimed_by, claimed_at
FROM transfer_jobs
WHERE state = 'RUNNING' AND claimed_at < now() - interval '15 minutes';
```

These reclaim automatically when the lease expires. If they do not, no worker is
running — `systemctl status 'c2w-worker@*'`.

## Verification mismatches

A recording that reaches `UPLOADED` but never `VERIFIED`:

```sql
SELECT id, source_size, destination_size, checksum_sha256, last_error_detail
FROM recordings WHERE state = 'UPLOADED' AND verified_at IS NULL;
```

Size mismatch with a clean transfer usually means the source object changed
mid-copy. Digest mismatch with matching sizes is more serious — check whether a
proxy or transcoder sits in the path.

The Sync status page shows this same breakdown with the fix for each cause.

## Requeue once you have fixed the cause

```sql
-- Put non-retryable failures back after fixing the underlying problem.
UPDATE transfer_jobs SET state = 'PENDING', attempts = 0, next_attempt_at = now()
WHERE state = 'FAILED' AND error_class = 'ACL_ERROR' AND connection_id = :n;
```

Requeueing without fixing the cause just re-fails and inflates `attempts` until
the job is permanently `FAILED`. Nightly reconciliation also re-queues anything
whose archive copy has gone missing, so a one-off blip usually needs no manual
action at all.

## Logs

```bash
journalctl -u 'c2w-worker@*' -p warning --since '1 hour ago'
journalctl -u c2w-api -f
```

Logs are structured JSON. Never paste log lines containing sealed credential
fields into a ticket — grep for `_sealed` before sharing.
