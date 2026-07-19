"""
MediaFire REST API — FastAPI · Railway-ready
"""
from __future__ import annotations
import asyncio, os, time
from typing import Optional

from fastapi import (
    FastAPI, HTTPException, UploadFile, File, Form,
    Query, Request,
)
from fastapi.responses import (
    JSONResponse, StreamingResponse, HTMLResponse, RedirectResponse,
)
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field

import tasks as T
from mediafire import fmt_size

# ──────────────────────────────────────────────────────────────────────────
# App — docs disabilitate di default, le rimappiamo sotto /api
# ──────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="MediaFire API",
    version="2.0.0",
    description="Async MediaFire REST API — upload, auto-registration, rotation",
    contact={"name": "errorcode808"},
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

API_KEY = os.environ.get("API_KEY", "")

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
    if email not in T._sessions:
        raise HTTPException(401, f"no active session for {email}. POST /auth/login first.")

# ══════════════════════════════════════════════════════════════════════════
#  ROOT + DOCS
# ══════════════════════════════════════════════════════════════════════════
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/api")


@app.get("/api/openapi.json", include_in_schema=False)
async def openapi_schema():
    return get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )


@app.get("/api/docs", include_in_schema=False)
async def swagger_ui():
    return get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title=f"{app.title} · Swagger",
    )


@app.get("/api/redoc", include_in_schema=False)
async def redoc_ui():
    return get_redoc_html(
        openapi_url="/api/openapi.json",
        title=f"{app.title} · ReDoc",
    )


# ──────────────────────────────────────────────────────────────────────────
# DOCUMENTAZIONE HTML custom su /api
# ──────────────────────────────────────────────────────────────────────────
DOCS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MediaFire API · Docs</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root{
    --bg:#0b0d10; --panel:#11151a; --border:#1e242c;
    --text:#e6edf3; --mute:#8b949e; --acc:#58a6ff;
    --ok:#3fb950; --wrn:#d29922; --err:#f85149; --hi:#bc8cff;
    --code:#161b22;
  }
  *{box-sizing:border-box}
  body{
    margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
      Roboto,Oxygen,Ubuntu,sans-serif;
    background:var(--bg);color:var(--text);line-height:1.55;
  }
  .wrap{max-width:1000px;margin:0 auto;padding:40px 24px 120px}
  header{
    padding-bottom:24px;border-bottom:1px solid var(--border);margin-bottom:32px;
  }
  h1{
    margin:0 0 8px;font-size:2rem;font-weight:700;
    background:linear-gradient(90deg,var(--acc),var(--hi));
    -webkit-background-clip:text;background-clip:text;color:transparent;
  }
  h2{
    margin:40px 0 16px;font-size:1.3rem;color:var(--text);
    padding-bottom:8px;border-bottom:1px solid var(--border);
  }
  h3{margin:24px 0 8px;font-size:1rem;color:var(--acc);font-weight:600}
  p,li{color:var(--text)}
  .mute{color:var(--mute);font-size:.9rem}
  code, pre{
    font-family:"SF Mono",Menlo,Consolas,monospace;
  }
  code{
    background:var(--code);padding:2px 6px;border-radius:4px;
    font-size:.85em;color:var(--hi);
  }
  pre{
    background:var(--code);padding:14px 16px;border-radius:8px;
    overflow-x:auto;border:1px solid var(--border);font-size:.85rem;
  }
  pre code{background:none;padding:0;color:var(--text)}
  .nav{
    display:flex;gap:8px;flex-wrap:wrap;margin-top:16px;
  }
  .nav a{
    background:var(--panel);border:1px solid var(--border);
    padding:6px 12px;border-radius:6px;color:var(--acc);
    text-decoration:none;font-size:.85rem;transition:.15s;
  }
  .nav a:hover{border-color:var(--acc);background:#151a22}
  .ep{
    background:var(--panel);border:1px solid var(--border);
    border-radius:8px;padding:16px 20px;margin:12px 0;
  }
  .method{
    display:inline-block;padding:2px 10px;border-radius:4px;
    font-size:.75rem;font-weight:700;letter-spacing:.5px;
    margin-right:10px;font-family:monospace;
  }
  .m-get   {background:#1f3a5f;color:#79b8ff}
  .m-post  {background:#1f4a2f;color:#7ee787}
  .m-delete{background:#5a1f1f;color:#ff7b72}
  .m-put   {background:#5f4a1f;color:#e3b341}
  .path{
    font-family:monospace;font-size:.95rem;color:var(--text);
    font-weight:600;
  }
  .desc{color:var(--mute);margin:8px 0 0;font-size:.9rem}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0}
  @media(max-width:700px){.grid{grid-template-columns:1fr}}
  .card{
    background:var(--panel);border:1px solid var(--border);
    border-radius:8px;padding:16px;
  }
  .card h3{margin-top:0}
  .badge{
    display:inline-block;padding:2px 8px;background:var(--hi);
    color:#1a1030;border-radius:4px;font-size:.7rem;font-weight:700;
    margin-left:6px;vertical-align:middle;
  }
  .kv{display:flex;gap:10px;margin:6px 0;font-size:.85rem}
  .kv .k{color:var(--mute);min-width:100px}
  .kv .v{color:var(--text);font-family:monospace}
  footer{
    margin-top:60px;padding-top:24px;border-top:1px solid var(--border);
    color:var(--mute);font-size:.85rem;text-align:center;
  }
  a{color:var(--acc)}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>MediaFire API</h1>
  <p class="mute">
    Async REST wrapper over MediaFire · auto-registration · multi-account
    upload rotation · job queue with live progress
  </p>
  <div class="nav">
    <a href="/api/docs">📘 Swagger UI</a>
    <a href="/api/redoc">📕 ReDoc</a>
    <a href="/api/openapi.json">🔧 OpenAPI JSON</a>
    <a href="/healthz">💚 Health</a>
  </div>
</header>

<h2>Quick start</h2>
<pre><code># 1. Crea account MediaFire automaticamente
curl -X POST __BASE__/auth/register
# → { "job_id": "...", "status": "pending", "poll": "/jobs/..." }

# 2. Monitora
curl __BASE__/jobs/&lt;job_id&gt;

# 3. Login (o usa l'account appena creato)
curl -X POST __BASE__/auth/login \
     -H "Content-Type: application/json" \
     -d '{"email":"...","password":"..."}'

# 4. Upload
curl -X POST __BASE__/files/&lt;email&gt;/upload \
     -F "files=@video.mp4" \
     -F "files=@doc.pdf"

# 5. Lista file + link
curl __BASE__/files/&lt;email&gt;
curl __BASE__/files/&lt;email&gt;/&lt;quickkey&gt;/links
</code></pre>

<div class="grid">
  <div class="card">
    <h3>🔐 Authentication</h3>
    <p class="desc">
      Se la env var <code>API_KEY</code> è impostata, ogni richiesta deve
      includere l'header:
    </p>
    <pre><code>X-API-Key: your-secret-key</code></pre>
    <p class="desc">Oppure query <code>?api_key=...</code></p>
  </div>
  <div class="card">
    <h3>⚙️ Jobs asincroni</h3>
    <p class="desc">
      Le operazioni pesanti (register, upload, wipe) ritornano
      subito un <code>job_id</code>. Monitora con
      <code>GET /jobs/&lt;id&gt;</code> o via SSE
      <code>GET /jobs/&lt;id&gt;/stream</code>.
    </p>
  </div>
</div>

<h2>🔐 Auth</h2>

<div class="ep">
  <div><span class="method m-post">POST</span><span class="path">/auth/register</span></div>
  <p class="desc">
    Crea un nuovo account MediaFire completamente automatico:
    email temporanea (mail.tm) → registrazione → verifica email → login →
    wipe iniziale. <span class="badge">async</span>
  </p>
  <p class="mute">Ritorna un <code>job_id</code>.</p>
</div>

<div class="ep">
  <div><span class="method m-post">POST</span><span class="path">/auth/login</span></div>
  <p class="desc">Login con credenziali MediaFire esistenti.</p>
  <pre><code>{ "email": "user@x.com", "password": "..." }</code></pre>
</div>

<h2>👤 Account</h2>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/accounts</span></div>
  <p class="desc">Lista di tutti gli account noti (dal file locale) + sessioni attive.</p>
</div>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/account/{email}/info</span></div>
  <p class="desc">Info account, display name, storage.</p>
</div>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/account/{email}/storage</span></div>
  <p class="desc">Storage usato / totale / libero.</p>
</div>

<h2>📁 Files</h2>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/files/{email}</span></div>
  <p class="desc">Lista file e cartelle. Query opzionale <code>?folder_key=...</code></p>
</div>

<div class="ep">
  <div><span class="method m-post">POST</span><span class="path">/files/{email}/upload</span></div>
  <p class="desc">
    Upload multipart di uno o più file. <span class="badge">async</span><br>
    Auto-rotation account se la quota si riempie durante l'upload.
  </p>
  <div class="kv"><div class="k">files</div><div class="v">multipart list</div></div>
  <div class="kv"><div class="k">folder</div><div class="v">form field · default: myfiles</div></div>
  <div class="kv"><div class="k">parallel</div><div class="v">form field · default: 4 (1-16)</div></div>
  <pre><code>curl -X POST __BASE__/files/user@x.com/upload \
     -F "files=@a.zip" -F "files=@b.zip" -F "parallel=4"</code></pre>
</div>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/files/{email}/{quickkey}/links</span></div>
  <p class="desc">Ottieni link di download (normal + direct).</p>
</div>

<div class="ep">
  <div><span class="method m-delete">DELETE</span><span class="path">/files/{email}/{quickkey}</span></div>
  <p class="desc">Elimina un singolo file.</p>
</div>

<h2>📂 Folders</h2>

<div class="ep">
  <div><span class="method m-post">POST</span><span class="path">/folders/{email}</span></div>
  <p class="desc">Crea una cartella.</p>
  <pre><code>{ "name": "mia-cartella", "parent": "myfiles" }</code></pre>
</div>

<div class="ep">
  <div><span class="method m-delete">DELETE</span><span class="path">/folders/{email}/{folderkey}</span></div>
  <p class="desc">Elimina una cartella.</p>
</div>

<h2>🗑 Wipe</h2>

<div class="ep">
  <div><span class="method m-post">POST</span><span class="path">/wipe/{email}</span></div>
  <p class="desc">
    ⚠️ Elimina TUTTI i file e le cartelle dell'account.
    <span class="badge">async</span>
  </p>
  <pre><code>{ "purge_trash": true }</code></pre>
</div>

<h2>⚡ Jobs</h2>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/jobs</span></div>
  <p class="desc">Lista job. Filtri: <code>?status=</code>, <code>?kind=</code>, <code>?limit=</code></p>
</div>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/jobs/{job_id}</span></div>
  <p class="desc">Dettaglio + progress log completo.</p>
</div>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/jobs/{job_id}/stream</span></div>
  <p class="desc">
    Server-Sent Events · streaming live del progress log.
    Eventi: <code>progress</code>, <code>done</code>, <code>error</code>.
  </p>
  <pre><code>curl -N __BASE__/jobs/&lt;job_id&gt;/stream</code></pre>
</div>

<h2>🔗 Links archive</h2>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/links</span></div>
  <p class="desc">
    Archivio di tutti i link generati. Filtri:
    <code>?email=</code>, <code>?limit=</code>, <code>?offset=</code>
  </p>
</div>

<h2>💚 System</h2>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/healthz</span></div>
  <p class="desc">Health check.</p>
</div>

<footer>
  MediaFire API v__VER__ · built with FastAPI · async edition by
  <b>errorcode808</b>
</footer>

</div>
</body>
</html>"""


@app.get("/api", response_class=HTMLResponse, include_in_schema=False)
async def api_docs(request: Request):
    base = str(request.base_url).rstrip("/")
    html = (DOCS_HTML
            .replace("__BASE__", base)
            .replace("__VER__", app.version))
    return HTMLResponse(html)


# ══════════════════════════════════════════════════════════════════════════
#  Health
# ══════════════════════════════════════════════════════════════════════════
@app.get("/healthz", tags=["system"])
async def healthz():
    return {"status": "ok", "time": int(time.time())}

# ══════════════════════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════════════════════
@app.post("/auth/register", tags=["auth"])
async def auth_register(request: Request):
    _check_key(request)
    jid = T.spawn_register()
    return _job_resp(jid)


@app.post("/auth/login", tags=["auth"])
async def auth_login(body: LoginBody, request: Request):
    _check_key(request)
    jid = T.spawn_login(body.email, body.password)
    return _job_resp(jid)

# ══════════════════════════════════════════════════════════════════════════
#  Account
# ══════════════════════════════════════════════════════════════════════════
@app.get("/accounts", tags=["account"])
async def list_accounts(request: Request):
    _check_key(request)
    accounts = T.load_accounts()
    active   = list(T._sessions.keys())
    return {
        "accounts": accounts,
        "active_sessions": active,
        "total": len(accounts),
    }


@app.get("/account/{email}/info", tags=["account"])
async def account_info(email: str, request: Request):
    _check_key(request)
    _require_session(email)
    try: return await T.direct_info(email)
    except Exception as e: raise HTTPException(400, str(e))


@app.get("/account/{email}/storage", tags=["account"])
async def account_storage(email: str, request: Request):
    _check_key(request)
    _require_session(email)
    try:
        info = await T.direct_info(email)
        return {
            "email": email,
            "used": info["used"], "total": info["total"], "free": info["free"],
            "used_fmt": info["used_fmt"], "total_fmt": info["total_fmt"],
            "free_fmt": info["free_fmt"], "pct": info["storage_pct"],
        }
    except Exception as e: raise HTTPException(400, str(e))

# ══════════════════════════════════════════════════════════════════════════
#  Files
# ══════════════════════════════════════════════════════════════════════════
@app.get("/files/{email}", tags=["files"])
async def list_files(
    email: str,
    request: Request,
    folder_key: str = Query("myfiles"),
):
    _check_key(request)
    _require_session(email)
    try:
        items = await T.direct_list(email, folder_key)
        return {
            "folder_key": folder_key,
            "total": len(items),
            "files":   [i for i in items if i["type"] == "file"],
            "folders": [i for i in items if i["type"] == "folder"],
        }
    except Exception as e: raise HTTPException(400, str(e))


@app.post("/files/{email}/upload", tags=["files"])
async def upload_files(
    email: str,
    request: Request,
    files: list[UploadFile] = File(...),
    folder: str = Form("myfiles"),
    parallel: int = Form(T.PARALLEL_DEFAULT),
):
    _check_key(request)
    _require_session(email)
    parallel = max(1, min(16, parallel))

    import uuid
    jid     = str(uuid.uuid4())
    job_dir = T.UPLOAD_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for uf in files:
        dest = job_dir / (uf.filename or f"file_{len(saved)}")
        dest.write_bytes(await uf.read())
        saved.append(str(dest))

    T._jobs[jid] = {
        "id": jid, "kind": "upload", "status": "pending",
        "progress": [], "result": None, "error": None,
        "created": time.time(), "updated": time.time(),
        "meta": {"email": email, "files": len(saved)},
    }
    asyncio.create_task(T._task_upload(jid, email, saved, folder, parallel))
    return JSONResponse(status_code=202, content={
        "job_id": jid, "status": "pending",
        "files": len(saved), "poll": f"/jobs/{jid}",
    })


@app.get("/files/{email}/{quickkey}/links", tags=["files"])
async def get_links(email: str, quickkey: str, request: Request):
    _check_key(request)
    _require_session(email)
    try: return await T.direct_links(email, quickkey)
    except Exception as e: raise HTTPException(400, str(e))


@app.delete("/files/{email}/{quickkey}", tags=["files"])
async def delete_file(email: str, quickkey: str, request: Request):
    _check_key(request)
    _require_session(email)
    try: return await T.direct_delete(email, quickkey, "file")
    except Exception as e: raise HTTPException(400, str(e))

# ══════════════════════════════════════════════════════════════════════════
#  Folders
# ══════════════════════════════════════════════════════════════════════════
@app.post("/folders/{email}", tags=["folders"])
async def create_folder(email: str, body: MkdirBody, request: Request):
    _check_key(request)
    _require_session(email)
    try: return await T.direct_mkdir(email, body.name, body.parent)
    except Exception as e: raise HTTPException(400, str(e))


@app.delete("/folders/{email}/{folderkey}", tags=["folders"])
async def delete_folder(email: str, folderkey: str, request: Request):
    _check_key(request)
    _require_session(email)
    try: return await T.direct_delete(email, folderkey, "folder")
    except Exception as e: raise HTTPException(400, str(e))

# ══════════════════════════════════════════════════════════════════════════
#  Wipe
# ══════════════════════════════════════════════════════════════════════════
@app.post("/wipe/{email}", tags=["wipe"])
async def wipe_account(email: str, body: WipeBody, request: Request):
    _check_key(request)
    _require_session(email)
    jid = T.spawn_wipe(email, body.purge_trash)
    return _job_resp(jid)

# ══════════════════════════════════════════════════════════════════════════
#  Jobs
# ══════════════════════════════════════════════════════════════════════════
@app.get("/jobs", tags=["jobs"])
async def list_all_jobs(
    request: Request,
    status: Optional[str] = Query(None),
    kind: Optional[str]   = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    _check_key(request)
    jobs = T.list_jobs()
    if status: jobs = [j for j in jobs if j["status"] == status]
    if kind:   jobs = [j for j in jobs if j["kind"]   == kind]
    jobs = jobs[:limit]
    return {
        "total": len(jobs),
        "jobs": [{k: v for k, v in j.items() if k != "progress"} for j in jobs],
    }


@app.get("/jobs/{jid}", tags=["jobs"])
async def get_job(
    jid: str,
    request: Request,
    include_progress: bool = Query(True),
):
    _check_key(request)
    j = _require_job(jid)
    out = dict(j)
    if not include_progress: out.pop("progress", None)
    return out


@app.get("/jobs/{jid}/stream", tags=["jobs"])
async def stream_job(jid: str, request: Request):
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

# ══════════════════════════════════════════════════════════════════════════
#  Links archive
# ══════════════════════════════════════════════════════════════════════════
@app.get("/links", tags=["links"])
async def all_links(
    request: Request,
    email: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    _check_key(request)
    links = T.load_links()
    if email: links = [l for l in links if l.get("account_email") == email]
    total = len(links)
    links = links[offset: offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "links": links}

# ══════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
