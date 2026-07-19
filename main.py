"""
mfreedownload API — async file relay
"""
from __future__ import annotations
import asyncio, os, re, time, uuid
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
from curl_cffi import requests as _cffi

import tasks as T

# ──────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="mfreedownload API",
    version="2.1.0",
    description="Async file relay · zero-config upload with auto account rotation",
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
class WipeBody(BaseModel):
    purge_trash: bool = True

def _job_resp(jid: str, code: int = 202):
    j = T.get_job(jid)
    return JSONResponse(status_code=code, content={
        "job_id": jid,
        "status": j["status"],  # type: ignore
        "poll":   f"/jobs/{jid}",
        "stream": f"/jobs/{jid}/stream",
    })

def _require_job(jid: str) -> dict:
    j = T.get_job(jid)
    if not j: raise HTTPException(404, f"job {jid} not found")
    return j

# ══════════════════════════════════════════════════════════════════════════
#  DOCS
# ══════════════════════════════════════════════════════════════════════════
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/api")


@app.get("/api/openapi.json", include_in_schema=False)
async def openapi_schema():
    return get_openapi(
        title=app.title, version=app.version,
        description=app.description, routes=app.routes,
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


DOCS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>mfreedownload · API</title>
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
      Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;
  }
  .wrap{max-width:960px;margin:0 auto;padding:48px 24px 120px}
  header{padding-bottom:28px;border-bottom:1px solid var(--border);margin-bottom:36px}
  h1{
    margin:0 0 6px;font-size:2.2rem;font-weight:800;letter-spacing:-.5px;
    background:linear-gradient(90deg,var(--acc),var(--hi));
    -webkit-background-clip:text;background-clip:text;color:transparent;
  }
  .tag{color:var(--mute);font-size:.95rem}
  h2{
    margin:44px 0 16px;font-size:1.35rem;
    padding-bottom:10px;border-bottom:1px solid var(--border);
  }
  h3{margin:20px 0 8px;font-size:1rem;color:var(--acc);font-weight:600}
  code{
    background:var(--code);padding:2px 6px;border-radius:4px;
    font-size:.88em;color:var(--hi);font-family:"SF Mono",Menlo,Consolas,monospace;
  }
  pre{
    background:var(--code);padding:16px 18px;border-radius:8px;
    overflow-x:auto;border:1px solid var(--border);font-size:.85rem;
    font-family:"SF Mono",Menlo,Consolas,monospace;
  }
  pre code{background:none;padding:0;color:var(--text)}
  .nav{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}
  .nav a{
    background:var(--panel);border:1px solid var(--border);
    padding:8px 14px;border-radius:6px;color:var(--acc);
    text-decoration:none;font-size:.85rem;transition:.15s;
  }
  .nav a:hover{border-color:var(--acc);background:#151a22}
  .ep{
    background:var(--panel);border:1px solid var(--border);
    border-radius:10px;padding:18px 22px;margin:14px 0;
  }
  .method{
    display:inline-block;padding:3px 12px;border-radius:5px;
    font-size:.75rem;font-weight:700;letter-spacing:.5px;
    margin-right:10px;font-family:monospace;
  }
  .m-get   {background:#1f3a5f;color:#79b8ff}
  .m-post  {background:#1f4a2f;color:#7ee787}
  .path{font-family:monospace;font-size:1rem;color:var(--text);font-weight:600}
  .desc{color:var(--mute);margin:10px 0 0;font-size:.92rem}
  .kv{display:flex;gap:12px;margin:6px 0;font-size:.85rem}
  .kv .k{color:var(--mute);min-width:110px}
  .kv .v{color:var(--text);font-family:monospace}
  .badge{
    display:inline-block;padding:2px 8px;background:var(--hi);
    color:#1a1030;border-radius:4px;font-size:.7rem;font-weight:700;
    margin-left:8px;vertical-align:middle;
  }
  .hero{
    background:linear-gradient(180deg,#151a22,var(--panel));
    border:1px solid var(--border);border-radius:12px;padding:24px 28px;
    margin:20px 0 32px;
  }
  .hero p{margin:0;color:var(--text);font-size:1.05rem}
  .hero .sub{color:var(--mute);font-size:.9rem;margin-top:8px}
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
  <h1>mfreedownload</h1>
  <div class="tag">zero-config async file relay · unlimited via auto-rotation</div>
  <div class="nav">
    <a href="/api/docs">📘 Swagger</a>
    <a href="/api/redoc">📕 ReDoc</a>
    <a href="/api/openapi.json">🔧 OpenAPI</a>
    <a href="/healthz">💚 Health</a>
  </div>
</header>

<div class="hero">
  <p>🚀 <b>Un solo endpoint.</b> Manda i tuoi file, ricevi i link.</p>
  <div class="sub">
    L'API gestisce autonomamente account, quote, rotation e retry.
    Nessuna configurazione richiesta.
  </div>
</div>

<h2>Quick start</h2>

<pre><code># Upload semplice (uno o più file)
curl -X POST __BASE__/upload \
     -F "files=@video.mp4" \
     -F "files=@archive.zip"

# ↓ Risposta immediata
# {
#   "job_id": "abc-123...",
#   "status": "pending",
#   "poll":   "/jobs/abc-123...",
#   "stream": "/jobs/abc-123.../stream"
# }

# Segui il progresso in tempo reale
curl -N __BASE__/jobs/abc-123.../stream

# Oppure polling classico
curl __BASE__/jobs/abc-123...

# ↓ Al termine, in "result.files" trovi:
# [
#   {
#     "filename": "video.mp4",
#     "size": 128394832,
#     "link":            "https://.../file/xxxx",
#     "short_download":  "/d/xxxx",
#     "short_stream":    "/s/xxxx",
#     "direct":          "https://.../direct/..."
#   }
# ]
</code></pre>

<h2>📤 Upload</h2>

<div class="ep">
  <div><span class="method m-post">POST</span><span class="path">/upload</span></div>
  <p class="desc">
    Uploada uno o più file. <span class="badge">async</span><br>
    L'API si occupa in autonomia di: creazione account temporanei,
    autenticazione, controllo quota, rotation quando serve, retry e generazione
    dei link finali.
  </p>
  <div class="kv"><div class="k">files</div><div class="v">multipart · uno o più file</div></div>
  <div class="kv"><div class="k">parallel</div><div class="v">form · default 4 (1–16)</div></div>
</div>

<h2>🔗 Short links</h2>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/d/{id}</span></div>
  <p class="desc">
    <b>Download diretto</b>. Redirect verso il CDN. Link permanente:
    il CDN dietro cambia da solo alla scadenza.<br>
    Es: <code>__BASE__/d/oewoa6wx7rvbm7p</code>
  </p>
</div>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/s/{id}</span></div>
  <p class="desc">
    <b>Streaming</b> per VLC, Wuffy, IINA, browser video ecc.
    Supporta Range requests.<br>
    Es: <code>vlc __BASE__/s/oewoa6wx7rvbm7p</code>
  </p>
</div>

<h2>⚡ Jobs</h2>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/jobs/{job_id}</span></div>
  <p class="desc">Stato e log completo. Al termine include <code>result.files</code>.</p>
</div>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/jobs/{job_id}/stream</span></div>
  <p class="desc">
    Server-Sent Events live. Eventi:
    <code>progress</code>, <code>done</code>, <code>error</code>.
  </p>
  <pre><code>curl -N __BASE__/jobs/&lt;job_id&gt;/stream</code></pre>
</div>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/jobs</span></div>
  <p class="desc">Lista tutti i job. Filtri: <code>?status=</code>, <code>?limit=</code>.</p>
</div>

<h2>📚 Links archive</h2>

<div class="ep">
  <div><span class="method m-get">GET</span><span class="path">/links</span></div>
  <p class="desc">Archivio persistente di tutti i link generati.
    Filtri: <code>?limit=</code>, <code>?offset=</code>.
  </p>
</div>

<h2>🔐 Auth (opzionale)</h2>
<p class="desc">
  Se l'ambiente definisce <code>API_KEY</code>, ogni richiesta richiede
  l'header <code>X-API-Key: your-key</code> (o query
  <code>?api_key=</code>).
</p>

<footer>
  mfreedownload · v__VER__ · async edition
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
#  Upload — endpoint unico, zero-config
# ══════════════════════════════════════════════════════════════════════════
@app.post("/upload", tags=["upload"],
          summary="Upload one or more files (fully automatic)")
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    parallel: int = Form(T.PARALLEL_DEFAULT),
):
    _check_key(request)
    parallel = max(1, min(16, parallel))

    if not files:
        raise HTTPException(400, "no files provided")

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
        "meta": {"files": len(saved)},
    }
    asyncio.create_task(T._task_upload_auto(jid, saved, parallel))

    return JSONResponse(status_code=202, content={
        "job_id": jid,
        "status": "pending",
        "files":  len(saved),
        "poll":   f"/jobs/{jid}",
        "stream": f"/jobs/{jid}/stream",
    })


# ══════════════════════════════════════════════════════════════════════════
#  Short links (/d/<id>, /s/<id>) — resolve CDN via scraping
# ══════════════════════════════════════════════════════════════════════════
_DIRECT_RE = re.compile(r'https?://download[^"\'<>\s]+', re.IGNORECASE)

# cache in-memory: quickkey → (direct_url, expires_epoch)
_DIRECT_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL   = 300  # 5 min


async def _resolve_direct(quickkey: str) -> str:
    """
    Prende il vero URL CDN (download1327.mediafire.com/...) per un quickkey
    scrappando la pagina share pubblica.
    """
    hit = _DIRECT_CACHE.get(quickkey)
    if hit and hit[1] > time.time():
        return hit[0]

    url = f"https://www.mediafire.com/file/{quickkey}"
    try:
        async with _cffi.AsyncSession(impersonate="chrome") as s:
            r = await s.get(url, allow_redirects=True, timeout=20)
            html = r.text
    except Exception as e:
        raise HTTPException(502, f"cannot resolve file: {e}")

    m = _DIRECT_RE.search(html)
    if not m:
        raise HTTPException(404, "file not found or link expired")

    direct = m.group(0).replace("&amp;", "&")
    _DIRECT_CACHE[quickkey] = (direct, time.time() + _CACHE_TTL)
    return direct


@app.get("/d/{quickkey}", tags=["links"],
         summary="Short download link (redirect to CDN)")
async def short_download(quickkey: str, request: Request):
    """
    Redirect 302 verso il vero URL CDN.
    Uso: https://<host>/d/<quickkey>
    """
    _check_key(request)
    direct = await _resolve_direct(quickkey)
    return RedirectResponse(url=direct, status_code=302)


@app.get("/s/{quickkey}", tags=["links"],
         summary="Short stream link (redirect for VLC/Wuffy/players)")
async def short_stream(quickkey: str, request: Request):
    """
    Redirect 302 al CDN, per streaming (VLC, Wuffy, browser video, …).
    Il CDN supporta Range requests, quindi seek/scrub funziona.
    """
    _check_key(request)
    direct = await _resolve_direct(quickkey)
    return RedirectResponse(url=direct, status_code=302)


# ══════════════════════════════════════════════════════════════════════════
#  Jobs
# ══════════════════════════════════════════════════════════════════════════
@app.get("/jobs", tags=["jobs"])
async def list_all_jobs(
    request: Request,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    _check_key(request)
    jobs = T.list_jobs()
    if status: jobs = [j for j in jobs if j["status"] == status]
    jobs = jobs[:limit]
    return {
        "total": len(jobs),
        "jobs":  [{k: v for k, v in j.items() if k != "progress"} for j in jobs],
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
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    _check_key(request)
    links = T.load_links()
    clean = [
        {k: v for k, v in l.items()
         if k not in ("account_email", "account_password")}
        for l in links
    ]
    # aggiungi short links se manca
    for l in clean:
        qk = l.get("quickkey")
        if qk:
            l.setdefault("short_download", f"/d/{qk}")
            l.setdefault("short_stream",   f"/s/{qk}")
    total = len(clean)
    clean = clean[offset: offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "links": clean}


# ══════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
