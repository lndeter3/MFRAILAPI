"""
MediaFire async client — ripristinato al formato esatto del CLI funzionante.
"""
from __future__ import annotations
import asyncio, hashlib, json, mimetypes, pickle, random, re
import string, time
from pathlib import Path
from typing import Optional, Callable

import requests as _http
from curl_cffi import requests as cffi

# ──────────────────────────────────────────────────────────────────────────
BASE = "https://www.mediafire.com"
APP  = "https://app.mediafire.com"
API  = f"{BASE}/api/1.5"
MAILTM_API   = "https://api.mail.tm"
REGISTER_URL = f"{BASE}/dynamic/register_gopro.php"
UPGRADE_URL  = f"{BASE}/upgrade/registration.php?pid=free"

IMPERSONATE      = "chrome"
CHUNK            = 4 * 1024 * 1024
RESUMABLE_TH     = CHUNK
DEFAULT_QUOTA    = 10 * 1024 ** 3
PARALLEL_DEFAULT = 4

API_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": APP, "Referer": f"{APP}/",
}

# ──────────────────────────────────────────────────────────────────────────
class MFError(Exception):
    def __init__(self, code, msg, payload=None):
        self.code = code; self.message = msg; self.payload = payload
        super().__init__(f"[{code}] {msg}")

class AuthError(MFError): pass
class QuotaError(MFError): pass

# ──────────────────────────────────────────────────────────────────────────
def rnd(n=10, cs=string.ascii_lowercase + string.digits):
    return "".join(random.choices(cs, k=n))

def sha256_bytes(d: bytes) -> str:
    return hashlib.sha256(d).hexdigest()

def sha256_file(path: Path, block=1 << 20, cb: Optional[Callable] = None) -> str:
    h = hashlib.sha256(); r = 0
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(block), b""):
            h.update(c); r += len(c)
            if cb: cb(r)
    return h.hexdigest()

def fmt_size(n: int) -> str:
    try: n = float(n)
    except Exception: n = 0
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024: return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"

async def to_thread(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

# ──────────────────────────────────────────────────────────────────────────
# TempMail
# ──────────────────────────────────────────────────────────────────────────
class TempMail:
    def __init__(self):
        self.s = _http.Session()
        self.address = self.password = self.token = None

    def _create_sync(self):
        domains = self.s.get(f"{MAILTM_API}/domains", timeout=15).json()
        domain = domains["hydra:member"][0]["domain"]
        self.address  = f"{rnd(12)}@{domain}"
        self.password = rnd(16) + "A1!"
        r = self.s.post(f"{MAILTM_API}/accounts",
            json={"address": self.address, "password": self.password}, timeout=15)
        r.raise_for_status()
        r = self.s.post(f"{MAILTM_API}/token",
            json={"address": self.address, "password": self.password}, timeout=15)
        r.raise_for_status()
        self.token = r.json()["token"]
        self.s.headers["Authorization"] = f"Bearer {self.token}"
        return self.address

    def _list_sync(self):
        return self.s.get(f"{MAILTM_API}/messages", timeout=15).json().get("hydra:member", [])

    def _get_sync(self, mid):
        return self.s.get(f"{MAILTM_API}/messages/{mid}", timeout=15).json()

    def _wait_sync(self, timeout=180, poll=5, log_fn=None):
        deadline = time.time() + timeout
        seen: set = set()
        while time.time() < deadline:
            for m in self._list_sync():
                if m["id"] in seen: continue
                seen.add(m["id"])
                subject = m.get("subject", "")
                if log_fn: log_fn(subject)
                if "welcome" in subject.lower(): continue
                if "verify" in subject.lower():
                    full = self._get_sync(m["id"])
                    body = (full.get("html") or [""])
                    body = body[0] if isinstance(body, list) else str(body)
                    body += "\n" + (full.get("text") or "")
                    mt = re.search(
                        r'https://email\.mediafire\.com/ls/click\?[^\s"\'<>]+', body)
                    if mt: return mt.group(0).replace("&amp;", "&")
            time.sleep(poll)
        raise TimeoutError("Email di verifica non ricevuta")

    async def create(self):
        return await to_thread(self._create_sync)

    async def wait_verification_link(self, timeout=180, poll=5, log_fn=None):
        return await to_thread(self._wait_sync, timeout, poll, log_fn)

# ──────────────────────────────────────────────────────────────────────────
def _register_sync(email, password, first_name, last_name) -> dict:
    s = cffi.Session(impersonate=IMPERSONATE)
    r = s.get(UPGRADE_URL); r.raise_for_status()
    m = (re.search(r'name=["\']security["\']\s+value=["\']([^"\']+)["\']', r.text)
         or re.search(r'security["\']?\s*[:=]\s*["\']([0-9]+\.[a-f0-9]+)["\']', r.text))
    if not m: raise RuntimeError("Token 'security' non trovato")
    sec = m.group(1)
    data = {
        "security": sec, "reg_first_name": first_name,
        "reg_last_name": last_name, "reg_email": email,
        "reg_display": "", "reg_pass": password,
        "agreement": "3.25", "pid": "free",
    }
    headers = {
        "origin": BASE, "referer": UPGRADE_URL,
        "content-type": "application/x-www-form-urlencoded",
    }
    r = s.post(REGISTER_URL, data=data, headers=headers)
    err = re.search(r'oErrorMessage\s*=\s*(\{[^}]*\})', r.text)
    if err and err.group(1).strip() not in ("{}", "{ }"):
        raise RuntimeError(f"Errore registrazione: {err.group(1)[:120]}")
    return {"status": r.status_code, "security_token": sec, "session": s}

def _verify_link_sync(session, link):
    return session.get(link, allow_redirects=True)

async def register(log_fn=None) -> dict:
    def _log(m):
        if log_fn: log_fn(m)

    pwd = "Mf" + rnd(12, string.ascii_letters + string.digits) + "!"
    fn  = rnd(6, string.ascii_lowercase).capitalize()
    ln  = rnd(7, string.ascii_lowercase).capitalize()

    mail = TempMail()
    _log("creating temp mailbox")
    email = await mail.create()
    _log(f"email: {email}")

    _log(f"registering {fn} {ln}")
    reg = await to_thread(_register_sync, email, pwd, fn, ln)
    reg_session = reg["session"]
    _log(f"account created (HTTP {reg['status']})")

    _log("waiting for verification email (180s max)")
    link = await mail.wait_verification_link(timeout=180, poll=5, log_fn=_log)
    _log("verification link received")

    await to_thread(_verify_link_sync, reg_session, link)
    _log("email verified")

    return {"email": email, "password": pwd, "first_name": fn, "last_name": ln}

# ──────────────────────────────────────────────────────────────────────────
# MediaFire client
# ──────────────────────────────────────────────────────────────────────────
class MediaFire:
    def __init__(self, session_file: Optional[str] = None):
        self.session_file = Path(session_file) if session_file else None
        self.s = cffi.AsyncSession(impersonate=IMPERSONATE)
        self.session_token: Optional[str] = None
        self.action_token_upload: Optional[str] = None
        self.action_token_upload_exp: float = 0
        self._email: Optional[str] = None
        self._password: Optional[str] = None
        self._lock = asyncio.Lock()
        self._last_upload_debug: str = ""

    def _dump(self) -> dict:
        return {
            "cookies": [
                {"name": c.name, "value": c.value,
                 "domain": c.domain, "path": c.path}
                for c in self.s.cookies.jar
            ],
            "session_token": self.session_token,
            "action_token_upload": self.action_token_upload,
            "action_token_upload_exp": self.action_token_upload_exp,
            "email": self._email,
            "password": self._password,
        }

    def _load_dict(self, d: dict):
        for c in d.get("cookies", []):
            self.s.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain"), path=c.get("path", "/"))
        self.session_token          = d.get("session_token")
        self.action_token_upload    = d.get("action_token_upload")
        self.action_token_upload_exp= d.get("action_token_upload_exp", 0)
        self._email    = d.get("email")
        self._password = d.get("password")

    def save(self):
        if self.session_file:
            self.session_file.write_bytes(pickle.dumps(self._dump()))

    def load(self) -> bool:
        if not self.session_file or not self.session_file.exists():
            return False
        try:
            self._load_dict(pickle.loads(self.session_file.read_bytes()))
            return bool(self.session_token)
        except Exception:
            return False

    def to_state(self) -> dict:
        return self._dump()

    @classmethod
    async def from_state(cls, state: dict) -> "MediaFire":
        mf = cls()
        mf._load_dict(state)
        return mf

    async def reset_session(self):
        try: await self.s.close()
        except Exception: pass
        self.s = cffi.AsyncSession(impersonate=IMPERSONATE)
        self.session_token = None
        self.action_token_upload = None
        self.action_token_upload_exp = 0

    async def _csrf(self) -> str:
        r = await self.s.get(f"{BASE}/login/")
        for pat in [
            r'security\s*[:=]\s*["\']([0-9]+\.[0-9a-f]+)["\']',
            r'name=["\']security["\']\s+value=["\']([^"\']+)["\']',
        ]:
            m = re.search(pat, r.text)
            if m: return m.group(1)
        raise AuthError(-1, "csrf not found")

    async def login(self, email: str, password: str, use_cache=True) -> str:
        self._email = email; self._password = password
        if use_cache and self.load():
            try:
                await self.user_info()
                return self.session_token  # type: ignore
            except MFError:
                pass
        await self.s.get(f"{BASE}/")
        sec = await self._csrf()
        r = await self.s.post(
            f"{BASE}/dynamic/client_login/mediafire.php",
            data={"security": sec, "login_email": email,
                  "login_pass": password, "login_remember": "true"},
            headers={"Origin": BASE, "Referer": f"{BASE}/login/"})
        ck = {c.name: c.value for c in self.s.cookies.jar}
        if ck.get("user", "x") == "x" or ck.get("session", "x") == "x":
            raise AuthError(-1, f"login failed: {r.text[:200]}")
        await self._bootstrap_token()
        self.save()
        return self.session_token  # type: ignore

    async def _bootstrap_token(self) -> str:
        r = await self.s.post(
            f"{BASE}/application/get_session_token.php",
            headers={"Origin": APP, "Referer": f"{APP}/", "Content-Length": "0"})
        token = None
        try:
            data = r.json()
            if isinstance(data, dict):
                resp = data.get("response", data)
                token = resp.get("session_token") if isinstance(resp, dict) else None
        except Exception: pass
        if not token:
            m = re.search(r"[0-9a-f]{120,}", r.text)
            if m: token = m.group(0)
        if not token:
            raise AuthError(-1, "session_token missing")
        self.session_token = token
        return token

    async def renew_token(self) -> str:
        async with self._lock:
            try:
                r = await self.s.post(
                    f"{API}/user/renew_session_token.php",
                    data={"session_token": self.session_token,
                          "response_format": "json"},
                    headers=API_HEADERS)
                new = (r.json().get("response") or {}).get("session_token")
                if new:
                    self.session_token = new; self.save(); return new
            except Exception: pass
            return await self.login(self._email, self._password, use_cache=False)  # type: ignore

    async def _api(self, path: str, **params) -> dict:
        if not self.session_token:
            raise AuthError(-1, "not authenticated")
        params.setdefault("response_format", "json")
        params["session_token"] = self.session_token
        url = f"{API}/{path.lstrip('/')}"
        r = await self.s.post(url, data=params, headers=API_HEADERS)
        try: data = r.json()
        except Exception: raise MFError(-1, f"non-JSON: {r.text[:200]}")
        resp = data.get("response", {})
        if resp.get("result") == "Error":
            code = str(resp.get("error", -1)); msg = resp.get("message", "")
            if code in ("105", "127"):
                await self.renew_token()
                params["session_token"] = self.session_token
                r = await self.s.post(url, data=params, headers=API_HEADERS)
                resp = r.json().get("response", {})
                if resp.get("result") == "Error":
                    code = str(resp.get("error", -1)); msg = resp.get("message", "")
                    if "storage" in msg.lower() or code in ("131", "159"):
                        raise QuotaError(code, msg)
                    raise MFError(resp.get("error"), msg)
            else:
                if "storage" in msg.lower() or code in ("131", "159"):
                    raise QuotaError(code, msg)
                raise MFError(resp.get("error"), msg)
        return resp

    async def _action_token(self, force_new: bool = False) -> str:
        if (not force_new and self.action_token_upload
                and time.time() < self.action_token_upload_exp - 300):
            return self.action_token_upload
        async with self._lock:
            if (not force_new and self.action_token_upload
                    and time.time() < self.action_token_upload_exp - 300):
                return self.action_token_upload
            if force_new:
                self.action_token_upload = None
                self.action_token_upload_exp = 0
            resp = await self._api("user/get_action_token.php",
                                   type="upload", lifespan=1440)
            tok = resp.get("action_token")
            if not tok: raise MFError(-1, "action_token missing")
            self.action_token_upload     = tok
            self.action_token_upload_exp = time.time() + 1440 * 60
            self.save()
            return tok

    async def user_info(self) -> dict:
        return await self._api("user/get_info.php")

    async def storage(self) -> tuple[int, int]:
        try:
            info = (await self.user_info()).get("user_info", {})
            used  = max(0, int(info.get("used_storage_size", 0)))
            total = int(info.get("storage_limit", 0)) or DEFAULT_QUOTA
            return used, total
        except Exception:
            return 0, DEFAULT_QUOTA

    async def free_bytes(self) -> int:
        used, total = await self.storage()
        return max(0, total - used)

    async def folder_content(self, fk="myfiles", ctype="files", chunk=1) -> dict:
        return await self._api(
            "folder/get_content.php",
            folder_key=fk, content_type=ctype,
            chunk=chunk, chunk_size=100, details="yes",
            order_by="name", order_direction="asc")

    async def folder_list_all(self, fk="myfiles") -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        for ct in ("folders", "files"):
            ch = 1
            while True:
                r = await self.folder_content(fk, ct, ch)
                content = r.get("folder_content", {})
                key = "folders" if ct == "folders" else "files"
                for it in content.get(key, []):
                    out.append((ct, it))
                if content.get("more_chunks", "no") != "yes": break
                ch += 1
        return out

    async def folder_create(self, name: str, parent="myfiles") -> dict:
        return await self._api(
            "folder/create.php",
            foldername=name, parent_key=parent, allow_duplicate_name="no")

    async def folder_delete(self, fk) -> dict:
        if isinstance(fk, list): fk = ",".join(fk)
        return await self._api("folder/delete.php", folder_key=fk)

    async def file_delete(self, qk) -> dict:
        if isinstance(qk, list): qk = ",".join(qk)
        return await self._api("file/delete.php", quick_key=qk)

    async def empty_trash(self) -> Optional[dict]:
        for endpoint in ("device/empty_trash.php", "user/empty_trash.php"):
            try: return await self._api(endpoint)
            except MFError: pass
        return None

    async def wipe_root(self, fk="myfiles", purge_trash=True) -> dict:
        items = await self.folder_list_all(fk)
        folder_keys = [it.get("folderkey") for ct, it in items
                       if ct == "folders" and it.get("folderkey")]
        file_keys   = [it.get("quickkey") for ct, it in items
                       if ct == "files"   and it.get("quickkey")]
        stats: dict = {"folders": 0, "files": 0, "errors": []}
        BATCH = 50
        for arr, kind, fn in [
            (folder_keys, "folders", self.folder_delete),
            (file_keys,   "files",   self.file_delete),
        ]:
            for i in range(0, len(arr), BATCH):
                batch = arr[i:i + BATCH]
                try:
                    await fn(batch); stats[kind] += len(batch)
                except Exception as e:
                    stats["errors"].append(f"{kind}[{i}]: {e}")
        if purge_trash:
            try:
                await self.empty_trash(); stats["trash_emptied"] = True
            except Exception as e:
                stats["trash_emptied"] = False
                stats["errors"].append(f"trash: {e}")
        return stats

    async def get_links(self, qk, lt="normal_download") -> list:
        if isinstance(qk, list): qk = ",".join(qk)
        r = await self._api("file/get_links.php", quick_key=qk, link_type=lt)
        return r.get("links", [])

    async def share(self, qk: str) -> str:
        l = await self.get_links(qk, "normal_download")
        if not l: raise MFError(-1, "no link")
        return l[0].get("normal_download") or l[0].get("view") or ""

    async def direct(self, qk: str) -> str:
        l = await self.get_links(qk, "direct_download")
        if not l: return ""
        return l[0].get("direct_download", "")

    async def upload_instant(self, fhash: str, fname: str,
                             size: int, fk="myfiles") -> Optional[dict]:
        try:
            r = await self._api("upload/instant.php",
                                filename=fname, folder_key=fk,
                                size=str(size), hash=fhash)
            return r if r.get("quickkey") else None
        except QuotaError: raise
        except MFError: return None

    async def upload_poll(self, key: str, timeout=120, interval=1.2) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = await self._api("upload/poll_upload.php", key=key)
            d = r.get("doupload", {})
            st = int(d.get("status", -1))
            qk = d.get("quickkey") or d.get("quick_key")
            if qk and st >= 11: return {"quickkey": qk, "raw": d}
            if st == 99:        return {"quickkey": qk, "raw": d}
            await asyncio.sleep(interval)
        raise MFError(-1, "poll timeout")

    # ── upload_simple: header ESATTAMENTE come nel CLI originale ──────────
    async def _upload_simple(self, path: Path, fname: str, fk: str,
                              fhash: str, atok: str,
                              on_progress: Callable) -> dict:
        size  = path.stat().st_size
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        with open(path, "rb") as f: data = f.read()
        r = await self.s.post(f"{API}/upload/simple.php",
            params={"session_token": self.session_token,
                    "action_token":  atok,
                    "folder_key":    fk,
                    "response_format": "json"},
            data=data,
            headers={"Content-Type": "application/octet-stream",
                     "X-Filehash": fhash,
                     "X-Filename": fname,
                     "X-Filetype": ctype,
                     "X-Filesize": str(size),
                     "Origin": APP, "Referer": f"{APP}/"})
        on_progress(size)
        try:
            resp = r.json().get("response", {})
        except Exception:
            raise MFError(-1, f"non-JSON: {r.text[:300]}")
        if resp.get("result") == "Error":
            code = str(resp.get("error")); msg = resp.get("message", "")
            self._last_upload_debug = f"simple resp: {resp}"
            if "storage" in msg.lower() or code in ("131", "159"):
                raise QuotaError(code, msg)
            raise MFError(resp.get("error"), msg)
        k = resp.get("doupload", {}).get("key")
        if not k: raise MFError(-1, "no upload key")
        return await self.upload_poll(k)

    # ── upload_resumable: header ESATTI + retry token + folder fallback ───
    async def _upload_resumable(self, path: Path, fname: str, fk: str,
                                 fhash: str, atok: str,
                                 on_progress: Callable) -> dict:
        size   = path.stat().st_size
        ctype  = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        url    = f"{API}/upload/resumable.php"

        cur_atok  = atok
        cur_fk    = fk
        upkey     = None
        uploaded  = 0

        def _params(tok, folder):
            return {"session_token": self.session_token,
                    "action_token":  tok,
                    "folder_key":    folder,
                    "response_format": "json"}

        with open(path, "rb") as f:
            uid = 0
            while True:
                chunk = f.read(CHUNK)
                if not chunk: break
                uhash = sha256_bytes(chunk)
                headers = {
                    "Content-Type": "application/octet-stream",
                    "X-Filehash":  fhash,
                    "X-Filename":  fname,
                    "X-Filetype":  ctype,
                    "X-Filesize":  str(size),
                    "X-Unit-Id":   str(uid),
                    "X-Unit-Hash": uhash,
                    "X-Unit-Size": str(len(chunk)),
                    "Origin": APP, "Referer": f"{APP}/",
                }

                data = None
                resp = None
                last_err = None
                tried_root_fallback = False

                for att in range(6):
                    try:
                        r = await self.s.post(url,
                                              params=_params(cur_atok, cur_fk),
                                              data=chunk, headers=headers)
                        raw_text = r.text
                        try:
                            data = r.json()
                        except Exception:
                            last_err = f"non-JSON (HTTP {r.status_code}): {raw_text[:300]}"
                            self._last_upload_debug = last_err
                            await asyncio.sleep(1.0 * (att + 1))
                            continue

                        resp = data.get("response", {})
                        if resp.get("result") == "Error":
                            code = str(resp.get("error"))
                            msg  = resp.get("message", "")
                            self._last_upload_debug = f"resp={resp}"
                            last_err = f"[{code}] {msg}"

                            if code in ("105", "127"):
                                await self.renew_token()
                                await asyncio.sleep(1.0)
                                continue
                            if code == "169":
                                # provo: nuovo action_token
                                if att < 2:
                                    await asyncio.sleep(2.0 + att)
                                    cur_atok = await self._action_token(force_new=True)
                                    continue
                                # provo: risolvo la root reale (a volte "myfiles" non funziona)
                                if not tried_root_fallback:
                                    tried_root_fallback = True
                                    try:
                                        info = await self._api("folder/get_info.php",
                                                               folder_key="myfiles")
                                        real_fk = (info.get("folder_info") or {}).get("folderkey")
                                        if real_fk and real_fk != cur_fk:
                                            cur_fk = real_fk
                                            self._last_upload_debug += f" | switched folder to {real_fk}"
                                            continue
                                    except Exception as e:
                                        last_err += f" | fk resolve err: {e}"
                                # ultimo tentativo: rinnovo tutto
                                await self.renew_token()
                                cur_atok = await self._action_token(force_new=True)
                                await asyncio.sleep(2.0)
                                continue
                            if "storage" in msg.lower() or code in ("131", "159"):
                                raise QuotaError(code, msg)
                            raise MFError(resp.get("error"), msg)
                        break  # success
                    except (QuotaError, MFError):
                        raise
                    except Exception as e:
                        last_err = str(e)
                        self._last_upload_debug = f"exc: {e}"
                        await asyncio.sleep(1.0 * (att + 1))
                else:
                    raise MFError(169,
                        f"chunk {uid} failed | last={last_err} | dbg={self._last_upload_debug}")

                k = (resp or {}).get("doupload", {}).get("key")
                if k: upkey = k
                uploaded += len(chunk); on_progress(uploaded)
                uid += 1

        if not upkey: raise MFError(-1, "no upload key after chunks")
        return await self.upload_poll(upkey)

    async def upload_file(self, path: Path, folder="myfiles",
                          on_progress: Optional[Callable] = None) -> dict:
        size  = path.stat().st_size
        fname = path.name
        prog  = on_progress or (lambda _: None)

        fhash = await to_thread(sha256_file, path, 1 << 20)
        hit   = await self.upload_instant(fhash, fname, size, folder)
        if hit:
            prog(size)
            return {"quickkey": hit["quickkey"], "method": "instant",
                    "size": size, "hash": fhash, "filename": fname}

        atok = await self._action_token()
        if size <= RESUMABLE_TH:
            res    = await self._upload_simple(path, fname, folder,
                                               fhash, atok, prog)
            method = "simple"
        else:
            res    = await self._upload_resumable(path, fname, folder,
                                                  fhash, atok, prog)
            method = "resumable"
        return {"quickkey": res.get("quickkey", ""), "method": method,
                "size": size, "hash": fhash, "filename": fname}

    async def close(self):
        try: await self.s.close()
        except Exception: pass
