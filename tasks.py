"""
tasks.py — job manager asincrono per mfreedownload API
"""
from __future__ import annotations
import asyncio, json as _json, os, time, uuid
from pathlib import Path
from typing import Optional

from mediafire import (
    MediaFire, MFError, AuthError, QuotaError,
    register, fmt_size, sha256_file, to_thread,
)

# ══════════════════════════════════════════════════════════════════════════
ACCOUNTS_FILE = os.environ.get("ACCOUNTS_FILE", "mf_accounts.txt")
LINKS_FILE    = os.environ.get("LINKS_FILE",    "mf_links.txt")

PARALLEL_DEFAULT = int(os.environ.get("UPLOAD_PARALLEL", "4"))
UPLOAD_DIR       = Path(os.environ.get("UPLOAD_DIR", "/tmp/mf_uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MIN_FREE_BYTES = int(os.environ.get("MIN_FREE_BYTES", 10 * 1024 * 1024))
WARMUP_DELAY   = float(os.environ.get("WARMUP_DELAY", "4.0"))


def save_account(email: str, password: str):
    with open(ACCOUNTS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{email}:{password}\n")


def load_accounts() -> list[dict]:
    out = []
    p = Path(ACCOUNTS_FILE)
    if not p.exists(): return out
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ":" in ln:
            e, pw = ln.split(":", 1)
            out.append({"email": e.strip(), "password": pw.strip()})
    return out


def append_link(record: dict):
    with open(LINKS_FILE, "a", encoding="utf-8") as f:
        f.write(_json.dumps(record) + "\n")


def load_links() -> list[dict]:
    p = Path(LINKS_FILE)
    if not p.exists(): return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try: out.append(_json.loads(ln))
        except Exception: pass
    return out


# ══════════════════════════════════════════════════════════════════════════
# Job store
# ══════════════════════════════════════════════════════════════════════════
_jobs: dict[str, dict] = {}


def _new_job(kind: str, meta: dict | None = None) -> str:
    jid = str(uuid.uuid4())
    _jobs[jid] = {
        "id":       jid,
        "kind":     kind,
        "status":   "pending",
        "progress": [],
        "result":   None,
        "error":    None,
        "created":  time.time(),
        "updated":  time.time(),
        "meta":     meta or {},
    }
    return jid


def get_job(jid: str) -> Optional[dict]:
    return _jobs.get(jid)


def list_jobs() -> list[dict]:
    return sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)


def _upd(jid: str, **kw):
    j = _jobs.get(jid)
    if not j: return
    j.update(kw); j["updated"] = time.time()


def _log(jid: str, msg: str):
    j = _jobs.get(jid)
    if not j: return
    j["progress"].append({"t": time.time(), "msg": msg})
    j["updated"] = time.time()


# ══════════════════════════════════════════════════════════════════════════
# Session store
# ══════════════════════════════════════════════════════════════════════════
_sessions: dict[str, dict] = {}


async def _get_mf(email: str) -> Optional[MediaFire]:
    state = _sessions.get(email)
    if not state: return None
    return await MediaFire.from_state(state)


def _save_mf(mf: MediaFire):
    if mf._email:
        _sessions[mf._email] = mf.to_state()


# ══════════════════════════════════════════════════════════════════════════
# Warm-up account (fondamentale per evitare errore 169 su account nuovi)
# ══════════════════════════════════════════════════════════════════════════
async def _warmup_account(mf: MediaFire, jid: str):
    """
    Dopo la verifica email il backend impiega qualche secondo ad "abilitare"
    davvero l'account per l'upload. Senza questo warm-up si prende [169].
    """
    _log(jid, f"warming up account ({WARMUP_DELAY:.1f}s)…")
    await asyncio.sleep(WARMUP_DELAY)
    try:
        await mf.user_info()
    except Exception as e:
        _log(jid, f"warmup user_info: {e}")
    try:
        await mf.folder_content("myfiles")
    except Exception as e:
        _log(jid, f"warmup folder_content: {e}")
    try:
        await mf._action_token(force_new=True)
    except Exception as e:
        _log(jid, f"warmup action_token: {e}")
    await asyncio.sleep(1.5)


# ══════════════════════════════════════════════════════════════════════════
# TASK: register
# ══════════════════════════════════════════════════════════════════════════
async def _task_register(jid: str):
    _upd(jid, status="running")
    try:
        def _log_fn(m): _log(jid, m)
        creds = await register(log_fn=_log_fn)
        save_account(creds["email"], creds["password"])
        _log(jid, f"account created: {creds['email']}")

        mf = MediaFire()
        await mf.login(creds["email"], creds["password"], use_cache=False)
        _log(jid, "login ok")
        await _warmup_account(mf, jid)
        try:
            stats = await mf.wipe_root("myfiles", purge_trash=True)
            _log(jid, f"wipe: {stats['folders']} folders, {stats['files']} files")
        except Exception as e:
            _log(jid, f"wipe warning: {e}")
        _save_mf(mf)
        await mf.close()

        _upd(jid, status="done", result={
            "email":    creds["email"],
            "password": creds["password"],
        })
    except Exception as e:
        _upd(jid, status="error", error=str(e))


def spawn_register() -> str:
    jid = _new_job("register")
    asyncio.create_task(_task_register(jid))
    return jid


# ══════════════════════════════════════════════════════════════════════════
# TASK: login
# ══════════════════════════════════════════════════════════════════════════
async def _task_login(jid: str, email: str, password: str):
    _upd(jid, status="running")
    try:
        mf = MediaFire()
        await mf.login(email, password, use_cache=False)
        save_account(email, password)
        used, total = await mf.storage()
        _save_mf(mf)
        await mf.close()
        _upd(jid, status="done", result={
            "email":     email,
            "used":      used,
            "total":     total,
            "free":      max(0, total - used),
            "used_fmt":  fmt_size(used),
            "total_fmt": fmt_size(total),
            "free_fmt":  fmt_size(max(0, total - used)),
        })
        _log(jid, f"login ok · free {fmt_size(max(0, total-used))}")
    except Exception as e:
        _upd(jid, status="error", error=str(e))


def spawn_login(email: str, password: str) -> str:
    jid = _new_job("login", {"email": email})
    asyncio.create_task(_task_login(jid, email, password))
    return jid


# ══════════════════════════════════════════════════════════════════════════
# Auto account picker
# ══════════════════════════════════════════════════════════════════════════
async def _pick_or_create_account(jid: str) -> MediaFire:
    """
    1. Sessioni in memoria con free > MIN_FREE_BYTES
    2. Account salvati → login + verifica
    3. Nuova registrazione
    """
    for email in list(_sessions.keys()):
        mf = await _get_mf(email)
        if not mf: continue
        try:
            free = await mf.free_bytes()
            if free > MIN_FREE_BYTES:
                _log(jid, f"reusing in-memory session · free {fmt_size(free)}")
                return mf
            await mf.close()
        except Exception:
            try: await mf.close()
            except Exception: pass

    for acc in load_accounts():
        if acc["email"] in _sessions:
            continue
        try:
            mf = MediaFire()
            await mf.login(acc["email"], acc["password"], use_cache=False)
            free = await mf.free_bytes()
            _save_mf(mf)
            if free > MIN_FREE_BYTES:
                _log(jid, f"logged into stored account · free {fmt_size(free)}")
                return mf
            await mf.close()
        except Exception:
            continue

    _log(jid, "no usable account · creating a fresh one")
    def _log_fn(m): _log(jid, m)
    creds = await register(log_fn=_log_fn)
    save_account(creds["email"], creds["password"])
    mf = MediaFire()
    await mf.login(creds["email"], creds["password"], use_cache=False)
    await _warmup_account(mf, jid)
    try:
        await mf.wipe_root("myfiles", purge_trash=True)
    except Exception: pass
    _save_mf(mf)
    _log(jid, "fresh account ready")
    return mf


async def _rotate_account(mf: MediaFire, jid: str) -> None:
    _log(jid, "rotating: current account is full")
    def _log_fn(m): _log(jid, m)
    creds = await register(log_fn=_log_fn)
    save_account(creds["email"], creds["password"])
    await mf.reset_session()
    await mf.login(creds["email"], creds["password"], use_cache=False)
    _log(jid, "rotated to fresh account")
    await _warmup_account(mf, jid)
    try:
        stats = await mf.wipe_root("myfiles", purge_trash=True)
        _log(jid, f"wiped: {stats['folders']} dirs + {stats['files']} files")
    except Exception as e:
        _log(jid, f"wipe warning: {e}")
    _save_mf(mf)


# ══════════════════════════════════════════════════════════════════════════
# Core: upload con rotation
# ══════════════════════════════════════════════════════════════════════════
async def _upload_with_rotation(
    mf: MediaFire,
    paths: list[Path],
    jid: str,
    folder: str = "myfiles",
    parallel: int = PARALLEL_DEFAULT,
) -> list[dict]:

    results: list[dict] = []
    queue       = list(paths)
    total_files = len(paths)
    done_count  = 0

    while queue:
        try:
            free = await mf.free_bytes()
        except Exception:
            free = 0
        _log(jid, f"free space: {fmt_size(free)}")

        batch: list[Path] = []
        rest:  list[Path] = []
        running = 0
        for p in queue:
            try: sz = p.stat().st_size
            except Exception: sz = 0
            if running + sz <= free and free > 0:
                batch.append(p); running += sz
            else:
                rest.append(p)

        if not batch:
            await _rotate_account(mf, jid)
            continue

        sem      = asyncio.Semaphore(parallel)
        counters = {"done": done_count}

        async def _run_one(path: Path) -> dict:
            async with sem:
                fname = path.name
                size  = path.stat().st_size
                _log(jid, f"↑ {fname} ({fmt_size(size)})")
                try:
                    def _prog(_b): pass
                    r = await mf.upload_file(path, folder=folder, on_progress=_prog)
                    qk = r["quickkey"]
                    try: link   = await mf.share(qk)
                    except Exception: link = ""
                    try: direct = await mf.direct(qk)
                    except Exception: direct = ""

                    record = {
                        "file":             str(path),
                        "filename":         fname,
                        "quickkey":         qk,
                        "method":           r["method"],
                        "size":             size,
                        "size_fmt":         fmt_size(size),
                        "hash":             r["hash"],
                        "link":             link,
                        "direct":           direct,
                        "account_email":    mf._email,
                        "account_password": mf._password,
                        "timestamp":        int(time.time()),
                    }
                    append_link(record)
                    counters["done"] += 1
                    _log(jid, f"✓ {fname} → {link} "
                              f"[{counters['done']}/{total_files}]")
                    return record
                except QuotaError as e:
                    _log(jid, f"quota on {fname}: {e}")
                    return {"file": str(path), "error": "quota", "_quota": True}
                except Exception as e:
                    _log(jid, f"✗ {fname}: {e}")
                    return {"file": str(path), "error": str(e)}

        tasks     = [asyncio.create_task(_run_one(p)) for p in batch]
        batch_res = await asyncio.gather(*tasks)

        quota_hits: list[Path] = []
        for path, r in zip(batch, batch_res):
            if r.get("_quota"):
                quota_hits.append(path)
            else:
                results.append(r)

        done_count = counters["done"]
        queue = quota_hits + rest

        if quota_hits or rest:
            await _rotate_account(mf, jid)

    return results


# ══════════════════════════════════════════════════════════════════════════
# TASK: upload (email/password espliciti)
# ══════════════════════════════════════════════════════════════════════════
async def _task_upload(
    jid: str,
    email: str,
    file_paths: list[str],
    folder: str,
    parallel: int,
):
    _upd(jid, status="running")
    paths = [Path(p) for p in file_paths]

    mf = await _get_mf(email)
    if mf is None:
        _upd(jid, status="error", error=f"no session for {email}")
        return
    try:
        await mf.user_info()
    except Exception:
        _upd(jid, status="error", error="session expired, please re-login")
        try: await mf.close()
        except Exception: pass
        return

    try:
        results = await _upload_with_rotation(mf, paths, jid, folder, parallel)
        _save_mf(mf)
        await mf.close()
        ok  = [r for r in results if r.get("link")]
        err = [r for r in results if r.get("error")]
        _upd(jid, status="done", result={
            "total":    len(paths),
            "uploaded": len(ok),
            "errors":   len(err),
            "files":    results,
        })
    except Exception as e:
        _upd(jid, status="error", error=str(e))
        try: await mf.close()
        except Exception: pass


def spawn_upload(
    email: str,
    file_paths: list[str],
    folder: str = "myfiles",
    parallel: int = PARALLEL_DEFAULT,
) -> str:
    jid = _new_job("upload", {"email": email, "files": len(file_paths)})
    asyncio.create_task(_task_upload(jid, email, file_paths, folder, parallel))
    return jid


# ══════════════════════════════════════════════════════════════════════════
# TASK: upload FULL-AUTO
# ══════════════════════════════════════════════════════════════════════════
async def _task_upload_auto(jid: str, file_paths: list[str], parallel: int):
    _upd(jid, status="running")
    paths = [Path(p) for p in file_paths]

    try:
        mf = await _pick_or_create_account(jid)
    except Exception as e:
        _upd(jid, status="error", error=f"account setup failed: {e}")
        return

    try:
        results = await _upload_with_rotation(mf, paths, jid, "myfiles", parallel)
        _save_mf(mf)
        await mf.close()

        clean_files = []
        for r in results:
            if r.get("link"):
                clean_files.append({
                    "filename": r.get("filename") or Path(r["file"]).name,
                    "size":     r.get("size"),
                    "size_fmt": r.get("size_fmt"),
                    "link":     r.get("link"),
                    "direct":   r.get("direct"),
                })
            else:
                clean_files.append({
                    "filename": Path(r.get("file", "?")).name,
                    "error":    r.get("error", "unknown"),
                })
        ok  = [f for f in clean_files if "link" in f]
        err = [f for f in clean_files if "error" in f]

        _upd(jid, status="done", result={
            "total":    len(paths),
            "uploaded": len(ok),
            "errors":   len(err),
            "files":    clean_files,
        })
    except Exception as e:
        _upd(jid, status="error", error=str(e))
        try: await mf.close()
        except Exception: pass

    try:
        job_dir = UPLOAD_DIR / jid
        if job_dir.exists():
            for f in job_dir.iterdir():
                try: f.unlink()
                except Exception: pass
            try: job_dir.rmdir()
            except Exception: pass
    except Exception: pass


# ══════════════════════════════════════════════════════════════════════════
# TASK: wipe
# ══════════════════════════════════════════════════════════════════════════
async def _task_wipe(jid: str, email: str, purge_trash: bool):
    _upd(jid, status="running")
    mf = await _get_mf(email)
    if mf is None:
        _upd(jid, status="error", error=f"no session for {email}")
        return
    try:
        stats = await mf.wipe_root("myfiles", purge_trash=purge_trash)
        _save_mf(mf)
        await mf.close()
        _upd(jid, status="done", result=stats)
    except Exception as e:
        _upd(jid, status="error", error=str(e))
        try: await mf.close()
        except Exception: pass


def spawn_wipe(email: str, purge_trash: bool = True) -> str:
    jid = _new_job("wipe", {"email": email})
    asyncio.create_task(_task_wipe(jid, email, purge_trash))
    return jid


# ══════════════════════════════════════════════════════════════════════════
# Helpers diretti
# ══════════════════════════════════════════════════════════════════════════
async def direct_info(email: str) -> dict:
    mf = await _get_mf(email)
    if not mf: raise RuntimeError(f"no session for {email}")
    try:
        info        = (await mf.user_info()).get("user_info", {})
        used, total = await mf.storage()
        _save_mf(mf)
        return {
            "email":       info.get("email"),
            "display_name":info.get("display_name"),
            "used":        used,
            "total":       total,
            "free":        max(0, total - used),
            "used_fmt":    fmt_size(used),
            "total_fmt":   fmt_size(total),
            "free_fmt":    fmt_size(max(0, total - used)),
            "storage_pct": round(used / total * 100, 2) if total else 0,
        }
    finally:
        await mf.close()


async def direct_list(email: str, folder_key: str = "myfiles") -> list[dict]:
    mf = await _get_mf(email)
    if not mf: raise RuntimeError(f"no session for {email}")
    try:
        items = await mf.folder_list_all(folder_key)
        _save_mf(mf)
        out = []
        for ct, it in items:
            if ct == "folders":
                out.append({
                    "type":    "folder",
                    "key":     it.get("folderkey"),
                    "name":    it.get("name"),
                    "created": it.get("created"),
                })
            else:
                sz = int(it.get("size", 0))
                out.append({
                    "type":     "file",
                    "key":      it.get("quickkey"),
                    "name":     it.get("filename"),
                    "size":     sz,
                    "size_fmt": fmt_size(sz),
                    "created":  it.get("created"),
                })
        return out
    finally:
        await mf.close()


async def direct_links(email: str, quickkey: str) -> dict:
    mf = await _get_mf(email)
    if not mf: raise RuntimeError(f"no session for {email}")
    try:
        normal = await mf.share(quickkey)
        direct = await mf.direct(quickkey)
        _save_mf(mf)
        return {"quickkey": quickkey, "normal": normal, "direct": direct}
    finally:
        await mf.close()


async def direct_mkdir(email: str, name: str, parent: str = "myfiles") -> dict:
    mf = await _get_mf(email)
    if not mf: raise RuntimeError(f"no session for {email}")
    try:
        r  = await mf.folder_create(name, parent)
        fk = r.get("folder_key") or (r.get("created_folder") or {}).get("folder_key")
        _save_mf(mf)
        return {"folder_key": fk, "name": name, "parent": parent}
    finally:
        await mf.close()


async def direct_delete(email: str, key: str, kind: str = "file") -> dict:
    mf = await _get_mf(email)
    if not mf: raise RuntimeError(f"no session for {email}")
    try:
        if kind == "folder": await mf.folder_delete(key)
        else:                await mf.file_delete(key)
        _save_mf(mf)
        return {"deleted": key, "type": kind}
    finally:
        await mf.close()
