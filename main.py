"""
MediaFire REST API — FastAPI · Railway-ready
============================================

Endpoints
─────────
Auth
  POST /auth/register                 → crea account MF (job asincrono)
  POST /auth/login                    → login con email+password (job)

Account
  GET  /account/{email}/info          → storage, display_name, ecc.
  GET  /account/{email}/storage       → usato/totale/libero
  GET  /accounts                      → lista account noti

Files
  GET  /files/{email}                 → lista root (o ?folder_key=...)
  POST /files/{email}/upload          → upload multipart (job asincrono)
  GET  /files/{email}/{quickkey}/links→ link normal + direct
  DELETE /files/{email}/{quickkey}    → elimina file

Folders
  POST /folders/{email}               → crea cartella
  DELETE /folders/{email}/{folderkey} → elimina cartella

Wipe
  POST /wipe/{email}                  → svuota account (job)

Jobs
  GET  /jobs                          → lista tutti i job
  GET  /jobs/{jid}                    → dettaglio + progress
  GET  /jobs/{jid}/stream             → SSE live progress

Links (archivio locale)
  GET  /links                         → tutti i link salvati

Health
  GET  /healthz
"""
from __future__ import annotations
import asyncio, os, shutil, time
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI, HTTPException, UploadFile, File, Form,
    Query, BackgroundTasks, Request,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import tasks as T
from mediafire import fmt_size

# ──────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="MediaFire API",
    version="2.0.0",
    description=__doc__,
    contact={"name": "errorcode808"},
)

API_KEY = os.environ.get("API_KEY", "")   # se vuoto, auth disabilitata

def _check_key(request: Request):
    if not API_KEY: return
    k = (request.headers.get("X-API-Key")
         or request.query_params.get("api_key", ""))
    if k != API_KEY:
        raise HTTPException(403, "invalid api key")

# ──────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────
class LoginBody(BaseModel):
    email:    str = Field(..., example="user@example.com")
    password: str = Field(..., example="MyP@ss123")

class MkdirBody(BaseModel):
    name:   str = Field(..., example="my-folder")
    parent: str = Field("myfiles", example="myfiles")

class DeleteBody(BaseModel):
    type: str = Field("file", pattern="^(file|folder)$")

class WipeBody(BaseModel):
    purge_trash: bool = True

def _job_resp(jid: str, code: int = 202):
    j = T.get_job(jid)
    return JSONResponse(status_code=code, content={
        "job_id": jid,
        "status": j["status"],   # type: ignore
        "poll":   f"/jobs/{jid}",
    })

def _require_job(jid: str) -> dict:
    j = T.get_job(jid)
    if not j: raise HTTPException(404, f"job {jid} not found")
    return j

def _require_session(email: str):
    """Verifica che esista una sessione attiva per l'email."""
    if email not in T._sessions:
        raise HTTPException(401, f"no active session for {email}. POST /auth/login first.")

# ──────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────
@app.get("/healthz", tags=["system"],
         summary="Health check",
         response_description="{'status':'ok'}")
async def healthz():
    return {"status": "ok", "time": int(time.time())}

# ──────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────
@app.post("/auth/register", tags=["auth"],
          summary="Crea un nuovo account MediaFire automaticamente",
          response_description="Job ID. Monitora con GET /jobs/{job_id}")
async def auth_register(request: Request):
    """
    Lancia in background:
    1. Crea casella email temporanea (mail.tm)
    2. Registra account su MediaFire
    3. Verifica email
    4. Login automatico
    5. Wipe account (parte pulita)

    Ritorna subito `job_id`. Usa `GET /jobs/{job_id}` per il risultato.
    """
    _check_key(request)
    jid = T.spawn_register()
    return _job_resp(jid)


@app.post("/auth/login", tags=["auth"],
          summary="Login con credenziali MediaFire esistenti",
          response_description="Job ID")
async def auth_login(body: LoginBody, request: Request):
    """
    Autentica un account MediaFire già esistente.
    La sessione viene mantenuta in memoria e identificata dall'email.
    """
    _check_key(request)
    jid = T.spawn_login(body.email, body.password)
    return _job_resp(jid)

# ──────────────────────────────────────────────────────────────────────────
# Account
# ──────────────────────────────────────────────────────────────────────────
@app.get("/accounts", tags=["account"],
         summary="Lista tutti gli account noti (dal file locale)")
async def list_accounts(request: Request):
    _check_key(request)
    accounts = T.load_accounts()
    active   = list(T._sessions.keys())
    return {
        "accounts": accounts,
        "active_sessions": active,
        "total": len(accounts),
    }


@app.get("/account/{email}/info", tags=["account"],
         summary="Info account: nome, storage, ecc.")
async def account_info(email: str, request: Request):
    """Richiede sessione attiva. Usa POST /auth/login prima."""
    _check_key(request)
    _require_session(email)
    try:
        return await T.direct_info(email)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/account/{email}/storage", tags=["account"],
         summary="Storage usato / totale / libero")
async def account_storage(email: str, request: Request):
    _check_key(request)
    _require_session(email)
    try:
        info = await T.direct_info(email)
        return {
            "email":     email,
            "used":      info["used"],
            "total":     info["total"],
            "free":      info["free"],
            "used_fmt":  info["used_fmt"],
            "total_fmt": info["total_fmt"],
            "free_fmt":  info["free_fmt"],
            "pct":       info["storage_pct"],
        }
    except Exception as e:
        raise HTTPException(400, str(e))

# ──────────────────────────────────────────────────────────────────────────
# Files
# ──────────────────────────────────────────────────────────────────────────
@app.get("/files/{email}", tags=["files"],
         summary="Lista file e cartelle")
async def list_files(
    email: str,
    request: Request,
    folder_key: str = Query("myfiles", description="Folder key da listare"),
):
    _check_key(request)
    _require_session(email)
    try:
        items = await T.direct_list(email, folder_key)
        files   = [i for i in items if i["type"] == "file"]
        folders = [i for i in items if i["type"] == "folder"]
        return {
            "folder_key": folder_key,
            "total":   len(items),
            "files":   files,
            "folders": folders,
        }
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/files/{email}/upload", tags=["files"],
          summary="Upload uno o più file (multipart/form-data)",
          response_description="Job ID asincrono")
async def upload_files(
    email: str,
    request: Request,
    files: list[UploadFile] = File(..., description="File da uploadare"),
    folder: str = Form("myfiles", description="Folder key di destinazione"),
    parallel: int = Form(T.PARALLEL_DEFAULT,
                         description="Numero di upload paralleli (1-16)"),
):
    """
    Salva i file caricati in `/tmp/mf_uploads/<job_id>/`
    e lancia il job di upload con auto-rotation degli account.

    Monitora il progresso con `GET /jobs/{job_id}`.
    Il risultato finale contiene i link per ogni file.
    """
    _check_key(request)
    _require_session(email)
    parallel = max(1, min(16, parallel))

    # salva i file upload su disco
    jid     = str(__import__("uuid").uuid4())
    job_dir = T.UPLOAD_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for uf in files:
        dest = job_dir / (uf.filename or f"file_{len(saved)}")
        content = await uf.read()
        dest.write_bytes(content)
        saved.append(str(dest))

    # crea il job manualmente con id predefinito
    T._jobs[jid] = {
        "id":       jid,
        "kind":     "upload",
        "status":   "pending",
        "progress": [],
        "result":   None,
        "error":    None,
        "created":  time.time(),
        "updated":  time.time(),
        "meta":     {"email": email, "files": len(saved)},
    }
    asyncio.create_task(
        T._task_upload(jid, email, saved, folder, parallel))

    return JSONResponse(status_code=202, content={
        "job_id":   jid,
        "status":   "pending",
        "files":    len(saved),
        "poll":     f"/jobs/{jid}",
        "cleanup":  f"temp files in /tmp/mf_uploads/{jid}",
    })


@app.get("/files/{email}/{quickkey}/links", tags=["files"],
         summary="Ottieni link di download (normal + direct)")
async def get_links(email: str, quickkey: str, request: Request):
    _check_key(request)
    _require_session(email)
    try:
        return await T.direct_links(email, quickkey)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/files/{email}/{quickkey}", tags=["files"],
            summary="Elimina un file")
async def delete_file(email: str, quickkey: str, request: Request):
    _check_key(request)
    _require_session(email)
    try:
        return await T.direct_delete(email, quickkey, "file")
    except Exception as e:
        raise HTTPException(400, str(e))

# ──────────────────────────────────────────────────────────────────────────
# Folders
# ──────────────────────────────────────────────────────────────────────────
@app.post("/folders/{email}", tags=["folders"],
          summary="Crea una nuova cartella")
async def create_folder(email: str, body: MkdirBody, request: Request):
    _check_key(request)
    _require_session(email)
    try:
        return await T.direct_mkdir(email, body.name, body.parent)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/folders/{email}/{folderkey}", tags=["folders"],
            summary="Elimina una cartella")
async def delete_folder(email: str, folderkey: str, request: Request):
    _check_key(request)
    _require_session(email)
    try:
        return await T.direct_delete(email, folderkey, "folder")
    except Exception as e:
        raise HTTPException(400, str(e))

# ──────────────────────────────────────────────────────────────────────────
# Wipe
# ──────────────────────────────────────────────────────────────────────────
@app.post("/wipe/{email}", tags=["wipe"],
          summary="Elimina tutto il contenuto dell'account (job asincrono)")
async def wipe_account(email: str, body: WipeBody, request: Request):
    """
    ⚠️ Operazione distruttiva irreversibile.
    Elimina tutti i file e le cartelle. Opzionalmente svuota il cestino.
    """
    _check_key(request)
    _require_session(email)
    jid = T.spawn_wipe(email, body.purge_trash)
    return _job_resp(jid)

# ──────────────────────────────────────────────────────────────────────────
# Jobs
# ──────────────────────────────────────────────────────────────────────────
@app.get("/jobs", tags=["jobs"],
         summary="Lista tutti i job")
async def list_all_jobs(
    request: Request,
    status: Optional[str] = Query(None,
        description="Filtra per status: pending|running|done|error"),
    kind: Optional[str]   = Query(None,
        description="Filtra per tipo: register|login|upload|wipe"),
    limit: int = Query(50, ge=1, le=500),
):
    _check_key(request)
    jobs = T.list_jobs()
    if status: jobs = [j for j in jobs if j["status"] == status]
    if kind:   jobs = [j for j in jobs if j["kind"]   == kind]
    jobs = jobs[:limit]
    # ometti progress log per la lista (troppo verboso)
    return {
        "total": len(jobs),
        "jobs": [
            {k: v for k, v in j.items() if k != "progress"}
            for j in jobs
        ],
    }


@app.get("/jobs/{jid}", tags=["jobs"],
         summary="Dettaglio e progress log di un job")
async def get_job(
    jid: str,
    request: Request,
    include_progress: bool = Query(True, description="Includi log di progresso"),
):
    _check_key(request)
    j = _require_job(jid)
    out = dict(j)
    if not include_progress: out.pop("progress", None)
    return out


@app.get("/jobs/{jid}/stream", tags=["jobs"],
         summary="Server-Sent Events: stream live del progress log")
async def stream_job(jid: str, request: Request):
    """
    Connettiti con `EventSource` o `curl -N`.
    Manda un evento `progress` per ogni nuovo messaggio di log,
    e un evento `done` o `error` quando il job finisce.

    ```
    curl -N http://localhost:8000/jobs/<jid>/stream
    ```
    """
    _check_key(request)
    _require_job(jid)

    async def _gen():
        sent = 0
        while True:
            j = T.get_job(jid)
            if not j: break
            prog = j["progress"]
            while sent < len(prog):
                entry = prog[sent]
                yield f"event: progress\ndata: {entry['msg']}\n\n"
                sent += 1
            if j["status"] in ("done", "error"):
                import json as _json
                payload = _json.dumps({
                    "status": j["status"],
                    "result": j.get("result"),
                    "error":  j.get("error"),
                })
                evt = "done" if j["status"] == "done" else "error"
                yield f"event: {evt}\ndata: {payload}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

# ──────────────────────────────────────────────────────────────────────────
# Links archive
# ──────────────────────────────────────────────────────────────────────────
@app.get("/links", tags=["links"],
         summary="Archivio di tutti i link generati (mf_links.txt)")
async def all_links(
    request: Request,
    email: Optional[str] = Query(None, description="Filtra per account email"),
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    _check_key(request)
    links = T.load_links()
    if email: links = [l for l in links if l.get("account_email") == email]
    total = len(links)
    links = links[offset: offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "links": links}

# ──────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)