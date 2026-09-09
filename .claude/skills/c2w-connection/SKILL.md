---
name: c2w-connection
description: Onboard a CommPeak S3 connection or Wasabi destination and run the connection self-test. Use when adding a new bucket, tenant or brand, when a connection shows ERROR/DEGRADED status, or when diagnosing "AccessDenied"/"InvalidAccessKeyId" from CommPeak or Wasabi.
---

# Onboarding a storage connection

## Before you start

Enter credentials only through `c2w-admin`, which prompts without echo and
seals them immediately under the brand's data key. Never put them in a file, a
commit, or a shell command's arguments.

Adding a connection performs **no** operation against CommPeak. The platform is
read-only there, and even the self-test only lists and reads.

## CommPeak source

Required fields, all from the customer's CommPeak account page:

| Field | Value |
|---|---|
| endpoint | `https://recordings.commpeak.com` (default; don't change without reason) |
| bucket | the account UUID from the CommPeak account page (a UUID, e.g. `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) |
| access key | the S3 account **Token** |
| secret key | the S3 account **Secret** (shown once, unrecoverable) |
| addressing | path-style, always |

```bash
c2w-admin brand add   --name "Go4Rex" --slug go4rex
c2w-admin tenant add  --brand go4rex --name "go4rex.td.commpeak.com" --slug go4rex-td
c2w-admin connection add --brand go4rex --tenant go4rex-td \
  --name "Go4Rex TD" --bucket <account-uuid>     # prompts for token and secret
c2w-admin connection list
```

## Read the self-test output

`run_source_probes` reports each check independently so you can tell the failure
modes apart:

| Check | Failing means |
|---|---|
| `s3_authentication` + `AUTH_ERROR` | wrong token/secret, or it has been rotated |
| `s3_authentication` + `ACL_ERROR` | **this server's public IP is not whitelisted** in the CommPeak Access Control List — the single most common onboarding failure |
| `s3_authentication` + `CONFIG_ERROR` | wrong bucket UUID, or virtual-host addressing leaked in |
| `bucket_discovery` empty | credentials fine, bucket genuinely has no year prefixes yet |
| `list_operation` ok but `download_test` fails | LIST granted, GET not — ask CommPeak to widen the account's permissions |

Get the server's public IP to hand to CommPeak with `curl -s https://api.ipify.org`.

## Wasabi destination

Pick the region closest to the brand's data-residency requirement; the endpoint
resolves from it via `WASABI_REGIONS`. One destination per brand minimum —
never point two brands at the same bucket prefix.

```bash
c2w-admin destination add --brand go4rex --provider wasabi \
  --region eu-central-1 --bucket go4rex-recordings --path-prefix archive
c2w-admin destination list
```

Until a destination exists, recordings accumulate as `DISCOVERED` and the
dashboard says so plainly. Nothing is lost — the scheduler queues them once
storage appears, with no re-scan.

## After a connection tests OK

1. Check `c2w-admin doctor` — it reports schema revision, brands, users,
   connections, destinations and whether transfers are on.
2. Adjust `retention.offload_after_days` if the brand differs from the 90-day
   default (`--brand <id>` sets a per-brand override).
3. Use the `c2w-backfill` skill before enabling transfers on a bucket with
   years of history.
