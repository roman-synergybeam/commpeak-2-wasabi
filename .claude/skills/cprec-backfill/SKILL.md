---
name: cprec-backfill
description: Plan and run a historical recording backfill from a CommPeak bucket to Wasabi, with priority ordering and bandwidth caps. Use when onboarding a bucket with existing history, when a backfill is too slow or saturating the uplink, or when estimating how long a migration will take.
---

# Planning a backfill

## Know the size first

The buckets in play are not small; a naive full walk is a multi-day operation.

Indicative sizes from the first deployment, largest first:

| Bucket | Objects | Size |
|---|---:|---:|
| main dialer | 12,935,564 | 5.7 TB |
| main PBX | 6,051,530 | 7.8 TB |
| secondary PBX | 276,449 | 235 GB |
| newer dialer | 51,503 | 150 GB |
| verification | 2,381 | 1.9 GB |

Run `cprec-admin connection list` for the actual buckets and their state.

Transfer time is bandwidth-bound, not object-bound: 13.9 TB is ~40 h at
100 MB/s and ~13 days at 12 MB/s. Compute the estimate before promising a date:

Nothing is copied at all until an archive destination exists and
`transfer.enabled` is true — inventory and CDR search work regardless, so the UI
is useful while storage is still being provisioned.

## Always start small

Onboard with the smallest bucket first -- a few thousand objects rather than
millions. It
exercise the whole path — inventory, correlation, transfer, verification,
playback — in minutes rather than days, and a configuration error surfaces
cheaply.

## Priority order

Newest first, always. Recent calls are the ones people actually ask to hear, and
the UI must be useful on day one while history drains behind it.

Priority is derived from each recording's age (`backfill_priority`), so the
current month drains ahead of years of history without anyone having to plan it.

Inventory is enumerated hour-prefix at a time and each completed hour is
recorded, so a scan interrupted after 8 hours of work resumes where it stopped
instead of restarting.

## Protect the uplink

Recordings are pulled over the same link that carries live calls. Cap it — these
are database settings, changed in the UI or on the command line, and they take
effect within seconds across every worker without a restart:

```bash
cprec-admin settings set transfer.bandwidth_limit_mbps 200
cprec-admin settings set source.concurrency_per_connection 5   # CommPeak's own recommendation
cprec-admin settings set transfer.concurrency_global 20
```

Raise concurrency only after watching `RATE_LIMIT` counts stay at zero. If
CommPeak starts returning `SlowDown`, you are past the useful limit — the
retry ladder will absorb it, but throughput drops.

## Watch it

The Sync status page shows queue depth, per-connection progress and failures by
cause. From the shell:

```bash
cprec-admin doctor
journalctl -u 'cprec-worker@*' -f
```

Rising `pending` with flat `transferred` means workers are blocked, not slow —
check `error_class` distribution with the `cprec-diagnose` skill.

## Do not

- Do not add a delete-at-source step. The platform is read-only against
  CommPeak and there is no code path to write there.
- Do not raise `source.concurrency_per_connection` above ~5 to "speed things
  up"; CommPeak documents 5 and the setting is capped at 10 for that reason.
- Do not restart a stuck backfill by clearing the inventory. Job leases expire
  and reclaim on their own; clearing inventory re-lists millions of objects.
