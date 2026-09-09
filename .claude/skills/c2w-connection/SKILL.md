---
name: c2w-connection
description: Add or fix a CommPeak account or archive storage in c2w. Use when onboarding a bucket, tenant or organisation, when an account shows an error, or when diagnosing AccessDenied / InvalidAccessKeyId from CommPeak or Wasabi.
---

# Adding an account

Everything here is done **in the console**, not on the command line. An
organisation has as many CommPeak accounts as it has PBXes and dialers, and as
many archive buckets as it needs; each has its own credentials.

Credentials are sealed under the organisation's data key before the row is
written, and are never sent back to a browser. A stored credential can be
tested but not read — which is also why the fix for a wrong one is to type a
new one, not to go and look at the old one.

## A CommPeak account

**CommPeak → Add a CommPeak account.** It asks for:

| Field | Where it comes from |
|---|---|
| Name | Yours to choose — how it reads on these pages |
| CommPeak domain | The PBX or dialer this bucket belongs to |
| Bucket | The account UUID on the CommPeak account page |
| S3 token / secret | Same page. The secret is shown once and cannot be recovered from CommPeak |
| Call records address | Usually the same domain. Without it, recordings are archived with no call details |
| Copies go to | Which archive storage this account's recordings go to |

The tenant is derived from the domain rather than asked for: a tenant is only
ever the domain a bucket belongs to, so asking separately asks the same
question twice.

Adding an account performs **no** operation against CommPeak. The platform is
read-only there, and even the test only lists and reads.

## Then press Test

Each check is reported separately, because telling these apart is most of the
work of getting a new account going:

| Failing check | What it means |
|---|---|
| `s3 authentication` — wrong credentials | The token or secret is wrong, or has been rotated |
| `s3 authentication` — address not allowed | **This server's public address is not on the CommPeak account's access list.** The most common cause by a distance |
| `s3 authentication` — misconfigured | Wrong bucket UUID, or the wrong address |
| `bucket discovery` empty | Credentials fine; the bucket genuinely has no recordings yet |
| `list operation` passes but `download test` fails | Listing is permitted, reading is not — ask CommPeak to widen the account |

Get the address to give CommPeak with `curl -s https://api.ipify.org`.

## Archive storage

**Archive → Add archive storage.** The region is a menu, so a mistyped region
cannot leave a bucket unreachable, and the address is worked out from it. The
bucket must already exist — the console does not create buckets.

Press **Test**. It writes a small object, reads it back and deletes it, because
listing a bucket succeeds with read-only keys and would then fail on the first
real copy.

## After it tests clean

1. Point the CommPeak account at the storage, if you did not when adding it.
2. Check the retention window under **Settings → Retention** — 90 days by
   default, and per-organisation.
3. Turn on **Settings → Archiving → Copy recordings to the archive**.
4. Read the `c2w-backfill` skill before enabling a bucket with years of
   history. Do not simply switch it on and let it walk millions of objects
   unprioritised.

## For scripting

`c2w-admin` does the same things without a browser, and prompts for
credentials without echo:

```bash
c2w-admin brand add --name "Go4Rex" --slug go4rex
c2w-admin connection list
c2w-admin destination list
c2w-admin doctor            # schema revision, counts, whether copying is on
```

Never put a credential in a file, a commit, or a shell command's arguments.
