#!/usr/bin/env python3
"""Upload relay for Synology Photos.

Accepts large files as tunnel-safe chunks (<= CHUNK_MAX_MB each), stages them
on the NAS, verifies, and uploads into Synology Photos AS THE REQUESTING USER
via the localhost webapi using a lent DSM session (sid + synotoken). Stores no
credentials of its own. Also serves the zero-install web uploader page.

Python 3.8 stdlib only. See docs/RELAY_SPEC.md and docs/DSM_API_NOTES.md.
"""
import hashlib
import http.client
import json
import mimetypes
import os
import re
import shutil
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, urlencode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "PORT": "5863",
    "DSM_URL": "https://127.0.0.1:5001",
    "STAGING_DIR": "./staging",
    "CHUNK_MAX_MB": "50",
    "USER_QUOTA_GB": "20",
    "GLOBAL_QUOTA_GB": "100",
    "MAX_FILE_GB": "30",
    "STALE_HOURS": "240",
    "VERIFY_TLS": "false",
    "SID_CACHE_SECONDS": "300",
    "INIT_RATE_LIMIT": "30",       # per INIT_RATE_WINDOW per client IP
    "INIT_RATE_WINDOW": "600",
    # -- SideStore/AltStore source (all optional; source is disabled until
    #    SOURCE_PUBLIC_URL and APP_BUNDLE_ID are set in relay.config) --------
    "IPA_DIR": "./ipa",            # drop <APP_NAME>-<version>.ipa (+ icon.png) here
    "SOURCE_PUBLIC_URL": "",       # e.g. https://upload.example.com (no trailing /)
    "SOURCE_NAME": "Family Apps",
    "SOURCE_IDENTIFIER": "",       # reverse-DNS id, e.g. com.example.family-source
    "APP_NAME": "PhotoRelay",
    "APP_BUNDLE_ID": "",           # e.g. com.example.photorelay
    "APP_DEVELOPER": "Self-hosted",
    "APP_DESCRIPTION": "Photo and video backup to the family NAS.",
    "APP_MIN_OS": "16.0",
    "APP_TINT": "",                # hex without '#', e.g. 0C1346
}

CFG = {}
MB = 1024 * 1024
GB = 1024 * MB

RELOGIN_CODES = {105, 106, 107, 119}
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_config(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip()
    cfg["PORT"] = int(cfg["PORT"])
    cfg["CHUNK_MAX"] = int(cfg["CHUNK_MAX_MB"]) * MB
    cfg["USER_QUOTA"] = int(cfg["USER_QUOTA_GB"]) * GB
    cfg["GLOBAL_QUOTA"] = int(cfg["GLOBAL_QUOTA_GB"]) * GB
    cfg["MAX_FILE"] = int(cfg["MAX_FILE_GB"]) * GB
    cfg["STALE_SECONDS"] = int(cfg["STALE_HOURS"]) * 3600
    cfg["SID_CACHE_SECONDS"] = int(cfg["SID_CACHE_SECONDS"])
    cfg["INIT_RATE_LIMIT"] = int(cfg["INIT_RATE_LIMIT"])
    cfg["INIT_RATE_WINDOW"] = int(cfg["INIT_RATE_WINDOW"])
    cfg["VERIFY_TLS"] = cfg["VERIFY_TLS"].lower() in ("true", "1", "yes")
    staging = cfg["STAGING_DIR"]
    if not os.path.isabs(staging):
        staging = os.path.join(os.path.dirname(os.path.abspath(path)), staging)
    cfg["STAGING"] = staging
    ipa_dir = cfg["IPA_DIR"]
    if not os.path.isabs(ipa_dir):
        ipa_dir = os.path.join(os.path.dirname(os.path.abspath(path)), ipa_dir)
    cfg["IPA_PATH"] = ipa_dir
    cfg["SOURCE_PUBLIC_URL"] = cfg["SOURCE_PUBLIC_URL"].rstrip("/")
    dsm = urlsplit(cfg["DSM_URL"])
    cfg["DSM_HOST"] = dsm.hostname
    cfg["DSM_PORT"] = dsm.port or (443 if dsm.scheme == "https" else 80)
    cfg["DSM_HTTPS"] = dsm.scheme == "https"
    return cfg


# ---------------------------------------------------------------- DSM client

def _dsm_conn():
    if CFG["DSM_HTTPS"]:
        if CFG["VERIFY_TLS"]:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(
            CFG["DSM_HOST"], CFG["DSM_PORT"], timeout=120, context=ctx)
    return http.client.HTTPConnection(CFG["DSM_HOST"], CFG["DSM_PORT"], timeout=120)


def dsm_api(params, sid=None, synotoken=None):
    """Urlencoded webapi call. Returns parsed JSON; raises OSError on I/O."""
    body = urlencode(params).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if sid:
        headers["Cookie"] = "id=" + sid
    if synotoken:
        headers["X-SYNO-TOKEN"] = synotoken
    conn = _dsm_conn()
    try:
        conn.request("POST", "/webapi/entry.cgi", body=body, headers=headers)
        resp = conn.getresponse()
        return json.loads(resp.read().decode("utf-8", "replace"))
    finally:
        conn.close()


def dsm_whoami(sid, synotoken):
    """Validate a lent session. Returns (username, None) or (None, dsm_error_code)."""
    reply = dsm_api({"api": "SYNO.Foto.UserInfo", "method": "me", "version": "1"},
                    sid=sid, synotoken=synotoken)
    if reply.get("success"):
        return reply["data"]["name"], None
    return None, reply.get("error", {}).get("code", -1)


def dsm_upload_item(name, mtime, chunk_paths, total_size, sid, synotoken):
    """Stream the assembled file (straight from chunk files, no extra copy)
    to SYNO.Foto.Upload.Item as the session's user. Verified format: see
    docs/DSM_API_NOTES.md 'Upload API — VERIFIED'."""
    boundary = "----relay" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    fields = [
        ("api", "SYNO.Foto.Upload.Item"),
        ("method", "upload"),
        ("version", "1"),
        ("name", json.dumps(name)),
        ("duplicate", json.dumps("ignore")),
        ("mtime", str(int(mtime))),
    ]
    head = b""
    for key, value in fields:
        head += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, key, value)).encode()
    head += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\""
             "\r\nContent-Type: %s\r\n\r\n"
             % (boundary, name.replace('"', "_"), ctype)).encode()
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    content_length = len(head) + total_size + len(tail)

    def body_iter():
        yield head
        for path in chunk_paths:
            with open(path, "rb") as fh:
                while True:
                    block = fh.read(256 * 1024)
                    if not block:
                        break
                    yield block
        yield tail

    headers = {
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(content_length),
        "Cookie": "id=" + sid,
        "X-SYNO-TOKEN": synotoken,
    }
    conn = _dsm_conn()
    try:
        conn.request("POST", "/webapi/entry.cgi", body=body_iter(), headers=headers)
        resp = conn.getresponse()
        return json.loads(resp.read().decode("utf-8", "replace"))
    finally:
        conn.close()


# ------------------------------------------------------------ session cache

_sid_cache = {}          # sid -> (username, expires_at)
_sid_lock = threading.Lock()


def validate_session(sid, synotoken):
    """Returns (username, None) or (None, http_status). Caches good sids."""
    if not sid or not synotoken:
        return None, 401
    now = time.time()
    with _sid_lock:
        hit = _sid_cache.get(sid)
        if hit and hit[1] > now:
            return hit[0], None
    try:
        user, err = dsm_whoami(sid, synotoken)
    except OSError as exc:
        log("DSM unreachable during sid validation: %s" % exc)
        return None, 503
    if user is None:
        return None, 401
    with _sid_lock:
        _sid_cache[sid] = (user, now + CFG["SID_CACHE_SECONDS"])
        if len(_sid_cache) > 1000:
            for key in [k for k, v in _sid_cache.items() if v[1] <= now]:
                del _sid_cache[key]
    return user, None


# ------------------------------------------------------------- rate limiter

_rate = {}               # ip -> [timestamps]
_rate_lock = threading.Lock()


def rate_limited(ip):
    now = time.time()
    with _rate_lock:
        stamps = [t for t in _rate.get(ip, []) if t > now - CFG["INIT_RATE_WINDOW"]]
        if len(stamps) >= CFG["INIT_RATE_LIMIT"]:
            _rate[ip] = stamps
            return True
        stamps.append(now)
        _rate[ip] = stamps
        return False


# ----------------------------------------------------------------- staging

_staging_lock = threading.Lock()


def upload_dir(upload_id):
    return os.path.join(CFG["STAGING"], upload_id)


def meta_path(upload_id):
    return os.path.join(upload_dir(upload_id), "meta.json")


def load_meta(upload_id):
    if not UPLOAD_ID_RE.match(upload_id or ""):
        return None
    try:
        with open(meta_path(upload_id)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_meta(meta):
    path = meta_path(meta["upload_id"])
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(meta, fh)
    os.replace(tmp, path)


def chunk_path(upload_id, index):
    return os.path.join(upload_dir(upload_id), "%06d.part" % index)


def received_chunks(upload_id):
    try:
        names = os.listdir(upload_dir(upload_id))
    except OSError:
        return []
    return sorted(int(n[:-5]) for n in names if n.endswith(".part"))


def done_dir():
    return os.path.join(CFG["STAGING"], ".done")


def tombstone_path(upload_id):
    return os.path.join(done_dir(), upload_id + ".json")


def load_tombstone(upload_id):
    """Result record of an already-completed upload, kept so a client whose
    connection dropped during /complete can retry and learn the outcome
    instead of re-uploading everything."""
    if not UPLOAD_ID_RE.match(upload_id or ""):
        return None
    try:
        with open(tombstone_path(upload_id)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_tombstone(upload_id, record):
    os.makedirs(done_dir(), exist_ok=True)
    tmp = tombstone_path(upload_id) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(record, fh)
    os.replace(tmp, tombstone_path(upload_id))


def num_chunks(size, chunk_size):
    return max(1, (size + chunk_size - 1) // chunk_size)


def expected_chunk_len(meta, index):
    size, chunk_size = meta["size"], meta["chunk_size"]
    last = num_chunks(size, chunk_size) - 1
    if index < last:
        return chunk_size
    return size - last * chunk_size


def iter_metas():
    try:
        ids = os.listdir(CFG["STAGING"])
    except OSError:
        return
    for upload_id in ids:
        meta = load_meta(upload_id)
        if meta:
            yield meta


def staging_usage():
    """Returns (per_user_bytes dict, total_bytes) of staged chunk data."""
    per_user = {}
    total = 0
    for meta in iter_metas():
        used = 0
        for index in received_chunks(meta["upload_id"]):
            try:
                used += os.path.getsize(chunk_path(meta["upload_id"], index))
            except OSError:
                pass
        per_user[meta["user"]] = per_user.get(meta["user"], 0) + used
        total += used
    return per_user, total


def find_resumable(user, filename, size, sha256, mtime):
    for meta in iter_metas():
        if meta["user"] != user or meta["filename"] != filename or meta["size"] != size:
            continue
        if sha256 and meta.get("sha256"):
            if meta["sha256"] == sha256:
                return meta
            continue
        if meta.get("mtime") == mtime:
            return meta
    return None


def evict_stale():
    cutoff = time.time() - CFG["STALE_SECONDS"]
    try:
        for name in os.listdir(done_dir()):
            path = os.path.join(done_dir(), name)
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass
    for meta in list(iter_metas()):
        directory = upload_dir(meta["upload_id"])
        try:
            newest = max(os.path.getmtime(os.path.join(directory, name))
                         for name in os.listdir(directory))
        except (OSError, ValueError):
            continue
        if newest < cutoff:
            shutil.rmtree(directory, ignore_errors=True)
            log("evicted stale upload %s (%s, %s)"
                % (meta["upload_id"], meta["user"], meta["filename"]))


def eviction_loop():
    while True:
        try:
            evict_stale()
        except Exception as exc:  # never kill the sweeper
            log("eviction sweep error: %s" % exc)
        time.sleep(3600)


def sanitize_filename(name):
    name = os.path.basename(name or "").strip()
    name = "".join(ch for ch in name if ch >= " " and ch != "/")
    return name[:255] or "upload.bin"


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PhotoRelay/1.0"
    timeout = 300

    # -- helpers ---------------------------------------------------------

    def client_ip(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def send_json(self, status, payload, close=False):
        """close=True: the request body was NOT (fully) consumed, so the
        connection must not be reused — leftover body bytes would be parsed
        as the next request on a keep-alive connection."""
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > 64 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, ValueError):
            return None

    def session_from_body(self, body):
        user, status = validate_session(body.get("sid"), body.get("synotoken"))
        if user is None:
            self.send_json(status, {"error": "invalid or expired DSM session"
                                    if status == 401 else "DSM unreachable"})
        return user

    def log_message(self, fmt, *args):  # default stderr spam -> our log, errors only
        pass

    # -- routing ---------------------------------------------------------

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/status":
            self.handle_status(url)
        elif url.path == "/api/whoami":
            self.handle_whoami()
        elif url.path in ("/", "/index.html"):
            self.serve_index()
        elif url.path == "/sidestore/source.json":
            self.serve_source_json()
        elif url.path.startswith("/ipa/"):
            self.serve_ipa(url.path[len("/ipa/"):])
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path == "/api/init":
            self.handle_init()
        elif url.path == "/api/complete":
            self.handle_complete()
        elif url.path == "/api/login":
            self.handle_login()
        else:
            self.send_json(404, {"error": "not found"}, close=True)

    def do_PUT(self):
        url = urlsplit(self.path)
        if url.path == "/api/chunk":
            self.handle_chunk(url)
        else:
            self.send_json(404, {"error": "not found"}, close=True)

    def do_DELETE(self):
        url = urlsplit(self.path)
        if url.path == "/api/upload":
            self.handle_cancel(url)
        else:
            self.send_json(404, {"error": "not found"})

    # -- endpoints -------------------------------------------------------

    def serve_index(self):
        path = os.path.join(BASE_DIR, "web", "index.html")
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_json(404, {"error": "uploader page not installed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- SideStore/AltStore source ----------------------------------------
    # Public by design: the IPA and source.json are just app binaries/metadata
    # (useless without DSM credentials). SideStore re-signs the IPA with each
    # installer's own Apple ID, so what is served here needs no signing state.

    IPA_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(ipa|png)$")

    def serve_ipa(self, name):
        if not self.IPA_NAME_RE.match(name) or name != os.path.basename(name):
            self.send_json(404, {"error": "not found"})
            return
        path = os.path.join(CFG["IPA_PATH"], name)
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            self.send_json(404, {"error": "not found"})
            return
        with fh:
            ctype = "image/png" if name.endswith(".png") else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            shutil.copyfileobj(fh, self.wfile, length=MB)

    def serve_source_json(self):
        if not CFG["SOURCE_PUBLIC_URL"] or not CFG["APP_BUNDLE_ID"]:
            self.send_json(404, {"error": "source not configured"})
            return
        base = CFG["SOURCE_PUBLIC_URL"]
        prefix = CFG["APP_NAME"] + "-"
        versions = []
        try:
            entries = os.listdir(CFG["IPA_PATH"])
        except OSError:
            entries = []
        for entry in entries:
            if not (entry.startswith(prefix) and entry.endswith(".ipa")):
                continue
            path = os.path.join(CFG["IPA_PATH"], entry)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            versions.append({
                "version": entry[len(prefix):-len(".ipa")],
                "date": time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)),
                "size": stat.st_size,
                "downloadURL": "%s/ipa/%s" % (base, entry),
                "minOSVersion": CFG["APP_MIN_OS"],
                "_mtime": stat.st_mtime,
            })
        versions.sort(key=lambda v: v.pop("_mtime"), reverse=True)
        app = {
            "name": CFG["APP_NAME"],
            "bundleIdentifier": CFG["APP_BUNDLE_ID"],
            "developerName": CFG["APP_DEVELOPER"],
            "localizedDescription": CFG["APP_DESCRIPTION"],
            "versions": versions,
        }
        if CFG["APP_TINT"]:
            app["tintColor"] = "#" + CFG["APP_TINT"]
        if os.path.exists(os.path.join(CFG["IPA_PATH"], "icon.png")):
            app["iconURL"] = base + "/ipa/icon.png"
        source = {
            "name": CFG["SOURCE_NAME"],
            "identifier": CFG["SOURCE_IDENTIFIER"] or CFG["APP_BUNDLE_ID"] + ".source",
            "apps": [app],
        }
        body = json.dumps(source, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_login(self):
        """CORS-free login proxy: forwards credentials to localhost DSM,
        returns sid+synotoken. Nothing is stored."""
        body = self.read_json_body()
        if body is None or not body.get("account") or not body.get("passwd"):
            self.send_json(400, {"error": "account and passwd required"}, close=True)
            return
        params = {
            "api": "SYNO.API.Auth", "method": "login", "version": "7",
            "account": body["account"], "passwd": body["passwd"],
            "format": "sid", "enable_syno_token": "yes",
        }
        if body.get("otp_code"):
            params["otp_code"] = body["otp_code"]
        try:
            reply = dsm_api(params)
        except OSError as exc:
            log("DSM unreachable during login: %s" % exc)
            self.send_json(503, {"error": "DSM unreachable"})
            return
        if reply.get("success"):
            log("login ok: %s from %s" % (body["account"], self.client_ip()))
            self.send_json(200, {"sid": reply["data"]["sid"],
                                 "synotoken": reply["data"].get("synotoken", "")})
        else:
            code = reply.get("error", {}).get("code", -1)
            log("login failed: %s from %s (dsm error %s)"
                % (body["account"], self.client_ip(), code))
            self.send_json(401, {"error": "DSM login failed", "dsm_code": code})

    def handle_whoami(self):
        user, status = validate_session(self.headers.get("X-Sid"),
                                        self.headers.get("X-Syno-Token"))
        if user is None:
            self.send_json(status, {"error": "invalid session"})
        else:
            self.send_json(200, {"user": user})

    def handle_init(self):
        if rate_limited(self.client_ip()):
            self.send_json(429, {"error": "too many init requests, slow down"})
            return
        body = self.read_json_body()
        if body is None:
            self.send_json(400, {"error": "invalid JSON body"}, close=True)
            return
        filename = sanitize_filename(body.get("filename"))
        size = body.get("size")
        mtime = body.get("mtime")
        sha256 = (body.get("sha256") or "").lower() or None
        if not isinstance(size, int) or size <= 0:
            self.send_json(400, {"error": "size must be a positive integer"})
            return
        if size > CFG["MAX_FILE"]:
            self.send_json(413, {"error": "file exceeds %dGB cap"
                                 % (CFG["MAX_FILE"] // GB)})
            return
        if not isinstance(mtime, int) or mtime <= 0:
            mtime = int(time.time())
        if sha256 and not re.match(r"^[0-9a-f]{64}$", sha256):
            self.send_json(400, {"error": "sha256 must be 64 hex chars"})
            return
        user = self.session_from_body(body)
        if user is None:
            return

        with _staging_lock:
            existing = find_resumable(user, filename, size, sha256, mtime)
            if existing:
                self.send_json(200, {
                    "upload_id": existing["upload_id"],
                    "chunk_size": existing["chunk_size"],
                    "received": received_chunks(existing["upload_id"]),
                })
                log("init resume %s: %s %s (%d bytes)"
                    % (existing["upload_id"], user, filename, size))
                return
            per_user, total = staging_usage()
            if per_user.get(user, 0) + size > CFG["USER_QUOTA"]:
                self.send_json(507, {"error": "your staging quota is full; "
                                     "finish or wait for pending uploads"})
                return
            if total + size > CFG["GLOBAL_QUOTA"]:
                self.send_json(507, {"error": "relay staging is full, try later"})
                return
            meta = {
                "upload_id": uuid.uuid4().hex,
                "user": user,
                "filename": filename,
                "size": size,
                "mtime": mtime,
                "sha256": sha256,
                "chunk_size": CFG["CHUNK_MAX"],
                "created": int(time.time()),
            }
            os.makedirs(upload_dir(meta["upload_id"]), exist_ok=True)
            save_meta(meta)
        log("init new %s: %s %s (%d bytes, %d chunks)"
            % (meta["upload_id"], user, filename, size,
               num_chunks(size, meta["chunk_size"])))
        self.send_json(200, {"upload_id": meta["upload_id"],
                             "chunk_size": meta["chunk_size"], "received": []})

    def handle_chunk(self, url):
        query = parse_qs(url.query)
        upload_id = (query.get("id") or [""])[0]
        try:
            index = int((query.get("n") or [""])[0])
        except ValueError:
            self.send_json(400, {"error": "n must be an integer"}, close=True)
            return
        meta = load_meta(upload_id)
        if meta is None:
            self.send_json(404, {"error": "unknown upload_id"}, close=True)
            return
        user, status = validate_session(self.headers.get("X-Sid"),
                                        self.headers.get("X-Syno-Token"))
        if user is None:
            self.send_json(status, {"error": "invalid session"}, close=True)
            return
        if user != meta["user"]:
            self.send_json(403, {"error": "upload belongs to another user"}, close=True)
            return
        if not 0 <= index < num_chunks(meta["size"], meta["chunk_size"]):
            self.send_json(400, {"error": "chunk index out of range"}, close=True)
            return
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self.send_json(411, {"error": "Content-Length required"}, close=True)
            return
        length = int(length_header)
        if length > CFG["CHUNK_MAX"]:
            self.send_json(413, {"error": "chunk exceeds %dMB"
                                 % (CFG["CHUNK_MAX"] // MB)}, close=True)
            return
        expected = expected_chunk_len(meta, index)
        if length != expected:
            self.send_json(409, {"error": "chunk %d must be %d bytes, got %d"
                                 % (index, expected, length)}, close=True)
            return
        tmp = chunk_path(upload_id, index) + ".tmp"
        digest = hashlib.sha256()
        written = 0
        try:
            with open(tmp, "wb") as fh:
                while written < length:
                    block = self.rfile.read(min(256 * 1024, length - written))
                    if not block:
                        break
                    fh.write(block)
                    digest.update(block)
                    written += len(block)
        except OSError:
            written = -1
        if written != length:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.send_json(400, {"error": "truncated chunk body"}, close=True)
            return
        try:  # upload may have been cancelled (dir removed) while we wrote
            os.replace(tmp, chunk_path(upload_id, index))
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.send_json(404, {"error": "upload was cancelled"})
            return
        self.send_json(200, {"received": received_chunks(upload_id),
                             "chunk_sha256": digest.hexdigest()})

    def handle_cancel(self, url):
        """Cancel an upload: delete its staged chunks and metadata.
        Idempotent — cancelling an unknown/already-gone id is a no-op."""
        user, status = validate_session(self.headers.get("X-Sid"),
                                        self.headers.get("X-Syno-Token"))
        if user is None:
            self.send_json(status, {"error": "invalid session"})
            return
        upload_id = (parse_qs(url.query).get("id") or [""])[0]
        meta = load_meta(upload_id)
        if meta is None:
            self.send_json(200, {"cancelled": False, "note": "nothing staged"})
            return
        if user != meta["user"]:
            self.send_json(403, {"error": "upload belongs to another user"})
            return
        shutil.rmtree(upload_dir(upload_id), ignore_errors=True)
        log("cancelled %s: %s %s" % (upload_id, user, meta["filename"]))
        self.send_json(200, {"cancelled": True})

    def handle_status(self, url):
        upload_id = (parse_qs(url.query).get("id") or [""])[0]
        meta = load_meta(upload_id)
        if meta is None:
            tomb = load_tombstone(upload_id)
            if tomb:
                self.send_json(200, {"state": "done",
                                     "result": tomb.get("result", {})})
                return
            self.send_json(404, {"error": "unknown upload_id"})
            return
        received = received_chunks(upload_id)
        total = num_chunks(meta["size"], meta["chunk_size"])
        self.send_json(200, {
            "received": received,
            "size": meta["size"],
            "chunk_size": meta["chunk_size"],
            "state": "complete" if len(received) == total else "partial",
        })

    def handle_complete(self):
        body = self.read_json_body()
        if body is None:
            self.send_json(400, {"error": "invalid JSON body"}, close=True)
            return
        meta = load_meta(body.get("upload_id", ""))
        if meta is None:
            tomb = load_tombstone(body.get("upload_id", ""))
            if tomb:
                user = self.session_from_body(body)
                if user is None:
                    return
                if user != tomb.get("user"):
                    self.send_json(403, {"error": "upload belongs to another user"})
                    return
                result = dict(tomb.get("result", {}))
                result["already_completed"] = True
                self.send_json(200, result)
                return
            self.send_json(404, {"error": "unknown upload_id"})
            return
        user = self.session_from_body(body)
        if user is None:
            return
        if user != meta["user"]:
            self.send_json(403, {"error": "upload belongs to another user"})
            return
        total = num_chunks(meta["size"], meta["chunk_size"])
        received = received_chunks(meta["upload_id"])
        if len(received) != total:
            missing = sorted(set(range(total)) - set(received))
            self.send_json(409, {"error": "chunks missing", "missing": missing})
            return
        paths = [chunk_path(meta["upload_id"], i) for i in range(total)]
        expected_sha = (body.get("sha256") or meta.get("sha256") or "").lower() or None
        if expected_sha:
            digest = hashlib.sha256()
            for path in paths:
                with open(path, "rb") as fh:
                    for block in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(block)
            if digest.hexdigest() != expected_sha:
                shutil.rmtree(upload_dir(meta["upload_id"]), ignore_errors=True)
                log("complete %s: sha256 MISMATCH, staging dropped"
                    % meta["upload_id"])
                self.send_json(422, {"error": "sha256 mismatch, restart upload"})
                return
        try:
            reply = dsm_upload_item(meta["filename"], meta["mtime"], paths,
                                    meta["size"], body["sid"], body["synotoken"])
        except OSError as exc:
            log("complete %s: DSM upload I/O error: %s" % (meta["upload_id"], exc))
            self.send_json(502, {"error": "Photos upload failed, retry /complete"})
            return
        if reply.get("success"):
            data = reply.get("data", {})
            result = {"action": data.get("action"), "id": data.get("id"),
                      "unit_id": data.get("unit_id")}
            # tombstone BEFORE responding: if the client's connection died
            # mid-/complete, its retry finds the result instead of a 404
            save_tombstone(meta["upload_id"],
                           {"user": user, "result": result, "ts": int(time.time())})
            shutil.rmtree(upload_dir(meta["upload_id"]), ignore_errors=True)
            log("complete %s: %s %s -> action=%s id=%s"
                % (meta["upload_id"], user, meta["filename"],
                   result["action"], result["id"]))
            self.send_json(200, result)
            return
        code = reply.get("error", {}).get("code", -1)
        if code == 620:  # unsupported file extension (verified: same bytes ok as .mp4)
            shutil.rmtree(upload_dir(meta["upload_id"]), ignore_errors=True)
            log("complete %s: unsupported type %s, staging dropped"
                % (meta["upload_id"], meta["filename"]))
            self.send_json(415, {"error": "Synology Photos does not support "
                                 "this file type (%s)" % meta["filename"],
                                 "dsm_code": 620})
            return
        if code in RELOGIN_CODES:
            with _sid_lock:
                _sid_cache.pop(body.get("sid"), None)
            self.send_json(401, {"error": "DSM session expired",
                                 "relogin": True, "dsm_code": code})
            return
        log("complete %s: Photos API error %s (staging kept)"
            % (meta["upload_id"], reply.get("error")))
        self.send_json(502, {"error": "Photos API error, retry /complete",
                             "dsm_error": reply.get("error")})


def main():
    global CFG
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "relay.config")
    CFG = load_config(config_path)
    os.makedirs(CFG["STAGING"], exist_ok=True)
    threading.Thread(target=eviction_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", CFG["PORT"]), Handler)
    server.daemon_threads = True
    log("relay listening on :%d, DSM at %s, staging %s"
        % (CFG["PORT"], CFG["DSM_URL"], CFG["STAGING"]))
    server.serve_forever()


if __name__ == "__main__":
    main()
