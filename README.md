# Synology Photos upload relay

A single-file Python relay that makes **large uploads to Synology Photos work
through Cloudflare Tunnel** (or any proxy that caps request bodies — Cloudflare's
free plan rejects bodies over 100MB, so every video larger than that fails in
the official Photos app when accessed via a tunnel).

The relay accepts files as **chunks** (50MB each by default), stages them on
the NAS, reassembles, and uploads into Synology Photos **as the requesting
user** via the localhost DSM API. It also serves a phone-friendly **web
uploader page** — no app install needed.

## Features

- Chunked, **resumable** uploads: interrupted transfers continue from the last
  received chunk (works across relay restarts; retrying a completed upload
  returns the recorded result instead of re-uploading).
- Uploads land in the correct user's personal space with correct ownership,
  timeline date (`mtime`), indexing, and dedup — because the final upload is
  made with that user's own DSM session.
- **No credentials stored, ever.** Clients log into DSM themselves and lend
  their session (`sid` + `synotoken`); the relay validates it against DSM
  before accepting a single byte. Knowing the relay URL is worthless without
  a valid DSM login.
- Web uploader at `/`: DSM login (incl. optional OTP), multi-file queue you
  can keep adding to mid-upload, smallest-first ordering, per-file progress
  (size + percent), cancel (deletes staged chunks), resume.
- Zero dependencies: Python 3.8+ standard library only. One file.
- Housekeeping: per-user and global staging quotas, stale-upload eviction
  (default 10 days idle), per-IP rate limiting on upload creation.

## Requirements

- Synology NAS on DSM 7 with the Synology Photos package enabled.
- Python 3 available over SSH (`python3 -V` — DSM 7 ships 3.8).
- A user account to run it as (no root needed).

## Install

1. Copy this directory to the NAS, e.g. `/volume1/homes/<you>/upload-relay/`:

   ```
   scp -O relay.py relay.config run.sh <you>@<nas>:upload-relay/
   scp -O web/index.html <you>@<nas>:upload-relay/web/
   ssh <you>@<nas> 'chmod 700 upload-relay && chmod +x upload-relay/run.sh'
   ```

2. Review `relay.config` (port, quotas, chunk size — defaults are sensible).

3. Start it: `cd upload-relay && setsid ./run.sh </dev/null >/dev/null 2>&1 &`
   Logs go to `relay.log`.

4. Start on boot: DSM **Control Panel → Task Scheduler → Create → Triggered
   Task → Boot-up**, run as your user:
   `/bin/sh /volume1/homes/<you>/upload-relay/run.sh`

5. Expose it (optional, for use outside your LAN): add a hostname in your
   Cloudflare Tunnel (or reverse proxy) pointing to `http://<nas-ip>:5863`.
   Chunks are 50MB, far below Cloudflare's 100MB body cap.

Check it's alive: `curl http://<nas-ip>:5863/healthz` → `ok`.

## Configuration (`relay.config`)

| Key | Default | Meaning |
|---|---|---|
| `PORT` | 5863 | Listen port |
| `DSM_URL` | `https://127.0.0.1:5001` | DSM webapi base (localhost on the NAS) |
| `STAGING_DIR` | `./staging` | Where chunks are staged |
| `CHUNK_MAX_MB` | 50 | Max chunk size (also the size clients must send) |
| `USER_QUOTA_GB` | 20 | Staged bytes allowed per user |
| `GLOBAL_QUOTA_GB` | 100 | Staged bytes allowed in total |
| `MAX_FILE_GB` | 30 | Sanity cap on a single file |
| `STALE_HOURS` | 240 | Idle time before unfinished uploads are evicted |
| `VERIFY_TLS` | false | Verify DSM's TLS cert (localhost cert rarely matches) |

## API

All JSON unless noted. Auth = a DSM session: `sid` + `synotoken` obtained from
`SYNO.API.Auth` (the page's login form does this via `POST /api/login`, a thin
proxy to DSM that stores nothing).

```
POST   /api/login     {account, passwd, otp_code?} → {sid, synotoken}
GET    /api/whoami    headers X-Sid, X-Syno-Token → {user}
POST   /api/init      {sid, synotoken, filename, size, mtime, sha256?}
                      → {upload_id, chunk_size, received:[...]}   (resume-aware)
PUT    /api/chunk?id=<upload_id>&n=<index>   raw bytes, headers X-Sid, X-Syno-Token
GET    /api/status?id=<upload_id>            → {received, size, state}
POST   /api/complete  {sid, synotoken, upload_id, sha256?}
                      → {action, id, unit_id}  (415 for unsupported file types)
DELETE /api/upload?id=<upload_id>            cancel: deletes staged chunks
GET    /healthz       → ok
GET    /              → web uploader
```

## Notes & limits

- Synology Photos rejects some extensions at the API level (DSM error 620,
  mapped to HTTP 415 here): `mkv webm ts vob rm jfif avif`. Convert those
  (e.g. to mp4) first — the official app can't upload them either.
- `duplicate=ignore` semantics: an exact name+content duplicate is skipped
  (returns the existing item); same name with different content is kept via
  rename.
- The relay trusts DSM for identity: the username always comes from DSM's
  answer to the lent session, never from the client.
