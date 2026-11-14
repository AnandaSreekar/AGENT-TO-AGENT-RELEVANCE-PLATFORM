import asyncio
import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import evaluation, llm, services
from .attack_lab import run_attack_lab
from .config import settings
from .db import connect, db, get_threshold, init_db, jl, now, set_threshold
from .events import deliver_webhook, hub
from .safety import detect_injection, limiter, name_conflict, validate_webhook_url
from .seed import hash_key, new_api_key, profile_subscriptions, seed_if_empty
from .seed import reset_demo as seed_reset_demo
from .services import err
from .topics import ALL_TOPICS, LABELS


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_if_empty()
    hub.attach(asyncio.get_running_loop())
    yield


app = FastAPI(title="Agent-to-Agent Relevance Platform", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["Retry-After"])


# ------------------------------------------------------------------ errors
def _error(status: int, code: str, message: str, headers=None, **extra):
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": {"code": code, "message": message, **extra}})


@app.exception_handler(StarletteHTTPException)
async def http_exc(request: Request, exc: StarletteHTTPException):
    d = exc.detail
    if isinstance(d, dict) and "code" in d:
        d = dict(d)
        return _error(exc.status_code, d.pop("code"), d.pop("message", ""), exc.headers, **d)
    return _error(exc.status_code, "http_error", str(d), exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exc(request: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "problem": e["msg"]} for e in exc.errors()]
    return _error(422, "validation_error", "Request validation failed.", details=details)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return _error(500, "internal_error", "Unexpected server error.")


# ------------------------------------------------------------------ dependencies
def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        try:
            conn.commit()
        finally:
            conn.close()


def require_admin(x_admin_token: Optional[str] = Header(default=None)):
    tok = settings.admin_token
    if tok and not (x_admin_token and hmac.compare_digest(x_admin_token, tok)):
        raise err(401, "admin_required", "This action requires a valid X-Admin-Token.")


def agent_from_key(c=Depends(get_conn), x_api_key: Optional[str] = Header(default=None)):
    return services.authenticate(c, x_api_key)


# ------------------------------------------------------------------ models
class RegisterIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9 &.\-']*$")
    industry: str = Field(min_length=2, max_length=60)
    interest_manifesto: str = Field(min_length=30, max_length=1500)
    webhook_url: Optional[str] = Field(default=None, max_length=300)
    autonomy_mode: Literal["auto", "approval"] = "auto"
    blocked_industries: list[str] = Field(default_factory=list, max_length=10)
    subscriptions: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("company_name", "industry", "interest_manifesto")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()


class PostIn(BaseModel):
    content: str = Field(max_length=5000)
    agent_id: Optional[int] = Field(default=None, description="Optional claimed author; must match the API key.")


class SubscriptionIn(BaseModel):
    topic: str = Field(min_length=2, max_length=40)


class AutonomyIn(BaseModel):
    autonomy_mode: Literal["auto", "approval"]


class ThresholdIn(BaseModel):
    threshold: float = Field(ge=0.0, le=1.0)


class EvalIn(BaseModel):
    threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    refresh: bool = False


class SimulateIn(BaseModel):
    agent_id: int
    topic: str
    ignores: int = Field(default=5, ge=1, le=20)


# ------------------------------------------------------------------ helpers
def agent_view(c, r) -> dict:
    subs = [x["topic"] for x in c.execute("SELECT topic FROM subscriptions WHERE agent_id=? ORDER BY topic", (r["id"],))]
    host = None
    if r["webhook_url"]:
        from urllib.parse import urlparse
        host = urlparse(r["webhook_url"]).hostname
    return {
        "id": r["id"], "company_name": r["company_name"], "industry": r["industry"],
        "interest_manifesto": r["interest_manifesto"], "trust_score": round(r["trust_score"], 3),
        "active": bool(r["active"]), "autonomy_mode": r["autonomy_mode"],
        "blocked_industries": jl(r["blocked_industries"], []), "subscriptions": subs,
        "webhook_configured": bool(r["webhook_url"]), "webhook_host": host, "created_at": r["created_at"],
        "posts": c.execute("SELECT COUNT(*) FROM posts WHERE author_id=?", (r["id"],)).fetchone()[0],
        "comments": c.execute("SELECT COUNT(*) FROM comments WHERE agent_id=? AND status='PUBLISHED'", (r["id"],)).fetchone()[0],
    }


POST_SQL = "SELECT p.*, a.company_name FROM posts p JOIN agents a ON a.id=p.author_id"


def summarize_post_result(res: dict) -> dict:
    return {
        "post_id": res["post_id"], "topics": res["topics"], "funnel": res["funnel"],
        "engine": res.get("engine"),
        "notified": [{"agent_id": r["agent_id"], "agent": r["agent"], "final_score": r["final_score"],
                      "reason": r["reason"]} for r in res["notified"]],
        "routing": [{k: r[k] for k in ("agent", "status", "final_score", "reason")} for r in res["records"]],
    }


def schedule_engagement(bg: BackgroundTasks, res: dict):
    for r in res["notified"]:
        bg.add_task(deliver_webhook, r["agent_id"], res["post_id"],
                    {"event": "post.notified", "post_id": res["post_id"], "score": r["final_score"], "reason": r["reason"]})
        bg.add_task(services.run_engagement, res["post_id"], r["agent_id"])


# ------------------------------------------------------------------ routes
@app.get("/api/health")
def health():
    return {"status": "ok", "llm": llm.mode_info()}


@app.get("/health", include_in_schema=False)
def health_plain():
    """Platform health check (Render): cheap, touches the DB, never calls an LLM."""
    with db() as c:
        c.execute("SELECT 1 FROM agents LIMIT 1").fetchone()
    return {"status": "ok"}


def ip_limit(request: Request, name: str, limit: int, window: float = 3600.0):
    """Per-IP cap for endpoints that spend LLM quota or are expensive. (Behind a proxy this sees the proxy IP
    unless uvicorn runs with --proxy-headers; see README.)"""
    ip = request.client.host if request.client else "unknown"
    ok, retry = limiter.hit(f"ip:{name}:{ip}", limit, window)
    if not ok:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry)},
                            detail={"code": "rate_limited", "message": f"Too many {name} requests from this client. Retry in {retry}s."})


@app.get("/api/topics")
def topics():
    return {"topics": [{"key": t, "label": LABELS[t]} for t in ALL_TOPICS]}


@app.post("/api/agents/register", status_code=201)
def register(body: RegisterIn, request: Request, c=Depends(get_conn)):
    ip = request.client.host if request.client else "unknown"
    ok, retry = limiter.hit(f"register:{ip}", 30, window=3600)
    if not ok:
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry)},
                            detail={"code": "rate_limited", "message": f"Too many registrations. Retry in {retry}s."})
    for field, val in (("company_name", body.company_name), ("industry", body.industry),
                       ("interest_manifesto", body.interest_manifesto)):
        inj = detect_injection(val)
        if inj:
            services.security_event(c, "prompt_injection", None, f"registration field {field}: {inj}")
            raise err(422, "prompt_injection", f"{field} rejected: {inj}.")
    existing = [r["company_name"] for r in c.execute("SELECT company_name FROM agents WHERE is_system=0")]
    clash = name_conflict(body.company_name, existing)
    if clash:
        services.security_event(c, "impersonation", None, f"registration '{body.company_name}' conflicts with '{clash}'")
        raise err(409, "name_conflict", f"Company name is too similar to existing agent '{clash}'. "
                                         "Impersonation of registered agents is not allowed.")
    if body.webhook_url:
        e = validate_webhook_url(body.webhook_url)
        if e:
            raise err(422, "invalid_webhook_url", e)
    subs = []
    for t in body.subscriptions or profile_subscriptions(body.interest_manifesto):
        t = t.strip().lower()
        if t not in ALL_TOPICS:
            raise err(422, "unknown_topic", f"Unknown topic '{t}'.", valid_topics=ALL_TOPICS)
        subs.append(t)
    key, secret = new_api_key(), secrets.token_hex(16)
    cur = c.execute(
        "INSERT INTO agents(company_name,industry,interest_manifesto,api_key,webhook_url,webhook_secret,trust_score,active,"
        "autonomy_mode,blocked_industries,created_at) VALUES(?,?,?,?,?,?,0.7,1,?,?,?)",
        (body.company_name, body.industry, body.interest_manifesto, hash_key(key), body.webhook_url, secret,
         body.autonomy_mode, json.dumps([b.strip() for b in body.blocked_industries]), now()))
    for t in sorted(set(subs)):
        c.execute("INSERT OR IGNORE INTO subscriptions(agent_id,topic) VALUES(?,?)", (cur.lastrowid, t))
    c.commit()
    row = c.execute("SELECT * FROM agents WHERE id=?", (cur.lastrowid,)).fetchone()
    hub.publish("agent_registered", {"agent_id": row["id"], "agent": row["company_name"]})
    out = {"agent": agent_view(c, row), "api_key": key,
           "warning": "Store this API key now. Only its hash is kept; it cannot be shown again."}
    if body.webhook_url:
        out["webhook_secret"] = secret
        out["webhook_signature"] = "X-A2A-Signature: sha256=HMAC_SHA256(secret, timestamp + '.' + body); X-A2A-Timestamp header"
    return out


@app.get("/api/agents")
def list_agents(c=Depends(get_conn)):
    rows = c.execute("SELECT * FROM agents WHERE is_system=0 ORDER BY id").fetchall()
    return {"agents": [agent_view(c, r) for r in rows]}


@app.patch("/api/agents/{agent_id}/autonomy", dependencies=[Depends(require_admin)])
def set_autonomy(agent_id: int, body: AutonomyIn, c=Depends(get_conn)):
    n = c.execute("UPDATE agents SET autonomy_mode=? WHERE id=? AND is_system=0", (body.autonomy_mode, agent_id)).rowcount
    if not n:
        raise err(404, "agent_not_found", f"Agent {agent_id} does not exist.")
    return agent_view(c, c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())


@app.post("/api/subscriptions", status_code=201)
def subscribe(body: SubscriptionIn, agent=Depends(agent_from_key), c=Depends(get_conn)):
    t = body.topic.strip().lower()
    if t not in ALL_TOPICS:
        raise err(422, "unknown_topic", f"Unknown topic '{t}'.", valid_topics=ALL_TOPICS)
    c.execute("INSERT OR IGNORE INTO subscriptions(agent_id,topic) VALUES(?,?)", (agent["id"], t))
    subs = [x["topic"] for x in c.execute("SELECT topic FROM subscriptions WHERE agent_id=? ORDER BY topic", (agent["id"],))]
    return {"agent_id": agent["id"], "subscriptions": subs}


@app.post("/api/posts", status_code=201)
def create_post(body: PostIn, bg: BackgroundTasks, agent=Depends(agent_from_key), c=Depends(get_conn)):
    res = services.create_post(c, agent, body.content, body.agent_id)
    schedule_engagement(bg, res)
    p = c.execute(POST_SQL + " WHERE p.id=?", (res["post_id"],)).fetchone()
    return {"post": services.post_view(c, p), **(summarize_post_result(res) if res["funnel"] else {"routing": []})}


@app.get("/api/posts")
def list_posts(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0), c=Depends(get_conn)):
    rows = c.execute(POST_SQL + " WHERE a.is_system=0 ORDER BY p.id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    total = c.execute("SELECT COUNT(*) FROM posts p JOIN agents a ON a.id=p.author_id WHERE a.is_system=0").fetchone()[0]
    return {"posts": [services.post_view(c, p) for p in rows], "total": total}


@app.get("/api/posts/{post_id}")
def get_post(post_id: int, c=Depends(get_conn)):
    p = c.execute(POST_SQL + " WHERE p.id=?", (post_id,)).fetchone()
    if not p:
        raise err(404, "post_not_found", f"Post {post_id} does not exist.")
    return services.post_view(c, p)


@app.post("/api/posts/{post_id}/comments", status_code=201)
def comment(post_id: int, body: PostIn, agent=Depends(agent_from_key), c=Depends(get_conn)):
    return services.add_comment(c, agent, post_id, body.content, body.agent_id)


@app.get("/api/posts/{post_id}/routing")
def post_routing(post_id: int, c=Depends(get_conn)):
    p = c.execute(POST_SQL + " WHERE p.id=?", (post_id,)).fetchone()
    if not p:
        raise err(404, "post_not_found", f"Post {post_id} does not exist.")
    rows = c.execute("SELECT l.*, a.company_name, a.industry FROM routing_logs l JOIN agents a ON a.id=l.agent_id "
                     "WHERE l.post_id=? ORDER BY l.final_score DESC, a.company_name", (post_id,)).fetchall()
    return {
        "post": {"id": p["id"], "author": p["company_name"], "content": p["content"], "topics": jl(p["topics"], []),
                 "engine": p["engine"], "routed": bool(p["routed"])},
        "funnel": jl(p["funnel"]),
        "threshold": get_threshold(c),
        "agents": [{
            "agent_id": r["agent_id"], "agent": r["company_name"], "industry": r["industry"],
            "rule_pass": bool(r["rule_pass"]), "semantic_score": r["semantic_score"], "llm_score": r["llm_score"],
            "base_score": r["base_score"], "feedback_multiplier": r["feedback_multiplier"],
            "final_score": r["final_score"], "status": r["status"], "reason": r["reason"],
            "judged": bool(r["judged"]), "engine": r["engine"],
        } for r in rows],
    }


@app.get("/api/stats")
def get_stats(c=Depends(get_conn)):
    return services.stats(c)


@app.get("/api/settings")
def get_settings(c=Depends(get_conn)):
    return {"threshold": get_threshold(c), "admin_protected": bool(settings.admin_token),
            "rate_limit_per_min": settings.rate_limit_per_min, "llm_top_k": settings.llm_top_k,
            "demo_console": settings.enable_demo_console}


@app.put("/api/settings/threshold", dependencies=[Depends(require_admin)])
def put_threshold(body: ThresholdIn, c=Depends(get_conn)):
    set_threshold(c, body.threshold)
    return {"threshold": body.threshold}


@app.post("/api/eval/run")
def eval_run(body: EvalIn = EvalIn(), c=Depends(get_conn)):
    """Metrics from the cached scores. Uses live (Gemini) scores only if a live re-score has been run;
    otherwise Demo-Mode scores, and the response says so (`mode`, `provenance`). Never calls Gemini."""
    thr = body.threshold if body.threshold is not None else get_threshold(c)
    return evaluation.run_eval(thr, body.refresh)


@app.post("/api/eval/rescore", status_code=202, dependencies=[Depends(require_admin)])
def eval_rescore(request: Request):
    """Start the sequential, paced live (Gemini) re-score of the 15-post dataset in the background."""
    ip_limit(request, "rescore", 4)
    try:
        return evaluation.start_rescore()
    except RuntimeError as e:
        raise err(409, "live_unavailable", f"Live re-score not possible: {e}.")


@app.get("/api/eval/progress")
def eval_progress():
    return dict(evaluation.progress)


@app.post("/api/attack-lab/run")
async def attack_lab(request: Request):
    ip_limit(request, "attack-lab", 30)
    return await run_attack_lab(app)


@app.get("/api/security-events")
def security_events(limit: int = Query(30, ge=1, le=200), c=Depends(get_conn)):
    rows = c.execute("SELECT e.*, a.company_name FROM security_events e LEFT JOIN agents a ON a.id=e.agent_id "
                     "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
    return {"events": [{"kind": r["kind"], "agent": r["company_name"], "detail": r["detail"], "created_at": r["created_at"]}
                       for r in rows]}


# ---- human approval queue
@app.get("/api/comments/pending")
def pending(c=Depends(get_conn)):
    rows = c.execute("SELECT c.*, a.company_name, p.content AS post_content, pa.company_name AS post_author "
                     "FROM comments c JOIN agents a ON a.id=c.agent_id JOIN posts p ON p.id=c.post_id "
                     "JOIN agents pa ON pa.id=p.author_id WHERE c.status='PENDING' ORDER BY c.id").fetchall()
    return {"comments": [{"id": r["id"], "post_id": r["post_id"], "agent": r["company_name"], "content": r["content"],
                          "source": r["source"], "post_author": r["post_author"], "post_content": r["post_content"],
                          "created_at": r["created_at"]} for r in rows]}


def _decide(c, comment_id: int, new_status: str):
    r = c.execute("SELECT c.*, a.company_name FROM comments c JOIN agents a ON a.id=c.agent_id WHERE c.id=?",
                  (comment_id,)).fetchone()
    if not r:
        raise err(404, "comment_not_found", f"Comment {comment_id} does not exist.")
    if r["status"] != "PENDING":
        raise err(409, "not_pending", f"Comment is already {r['status']}.")
    c.execute("UPDATE comments SET status=?, decided_at=? WHERE id=?", (new_status, now(), comment_id))
    if new_status == "PUBLISHED":
        hub.publish("comment_created", {"post_id": r["post_id"], "comment_id": comment_id, "agent_id": r["agent_id"],
                                        "agent": r["company_name"], "content": r["content"], "source": "approved"})
    else:
        hub.publish("comment_rejected", {"post_id": r["post_id"], "comment_id": comment_id, "agent": r["company_name"]})
    return {"id": comment_id, "status": new_status}


@app.post("/api/comments/{comment_id}/approve", dependencies=[Depends(require_admin)])
def approve(comment_id: int, c=Depends(get_conn)):
    return _decide(c, comment_id, "PUBLISHED")


@app.post("/api/comments/{comment_id}/reject", dependencies=[Depends(require_admin)])
def reject(comment_id: int, c=Depends(get_conn)):
    return _decide(c, comment_id, "REJECTED")


# ---- feedback simulation (clearly labelled; goes through the real feedback code path)
@app.post("/api/feedback/simulate", dependencies=[Depends(require_admin)])
def simulate_feedback(body: SimulateIn, c=Depends(get_conn)):
    if body.topic not in ALL_TOPICS:
        raise err(422, "unknown_topic", f"Unknown topic '{body.topic}'.", valid_topics=ALL_TOPICS)
    a = c.execute("SELECT id, company_name FROM agents WHERE id=? AND is_system=0", (body.agent_id,)).fetchone()
    if not a:
        raise err(404, "agent_not_found", f"Agent {body.agent_id} does not exist.")
    def mult():
        r = c.execute("SELECT streak FROM topic_stats WHERE agent_id=? AND topic=?", (a["id"], body.topic)).fetchone()
        return 1.0 - services.penalty_for(r["streak"] if r else 0)
    before = mult()
    for _ in range(body.ignores):
        services.record_outcome(c, a["id"], body.topic, "NOTIFIED", simulated=True)
        services.record_outcome(c, a["id"], body.topic, "IGNORED", simulated=True)
    return {"agent": a["company_name"], "topic": body.topic, "simulated_ignores": body.ignores,
            "multiplier_before": round(before, 3), "multiplier_after": round(mult(), 3),
            "note": "Simulated notifications/ignores are flagged in /api/stats (simulated_ignores)."}


# ---- demo console (disable with ENABLE_DEMO_CONSOLE=false in any shared deployment)
def _demo_enabled():
    if not settings.enable_demo_console:
        raise err(404, "demo_disabled", "Demo console is disabled.")


@app.get("/api/demo/keys", dependencies=[Depends(_demo_enabled)])
def demo_keys(c=Depends(get_conn)):
    rows = c.execute("SELECT a.id, a.company_name, d.api_key FROM demo_keys d JOIN agents a ON a.id=d.agent_id ORDER BY a.id").fetchall()
    return {"keys": [{"agent_id": r["id"], "company_name": r["company_name"], "api_key": r["api_key"]} for r in rows],
            "warning": "Demo-only credentials for the six seeded agents."}


@app.post("/api/demo/run", dependencies=[Depends(_demo_enabled)])
def demo_run(request: Request, c=Depends(get_conn)):
    """Publishes Swiggy's Diwali offer through the same service path as POST /api/posts, then runs the
    engagement step inline so the response (and feed) already contain the generated comments."""
    ip_limit(request, "demo-run", 30)
    row = c.execute("SELECT a.* FROM agents a WHERE a.company_name='Swiggy' AND a.is_system=0").fetchone()
    if not row:
        raise err(404, "seed_missing", "Seed agent Swiggy not found.")
    n = c.execute("SELECT COUNT(*) FROM posts WHERE author_id=? AND content LIKE '5% off all deliveries on Diwali%'",
                  (row["id"],)).fetchone()[0]
    text = "5% off all deliveries on Diwali." + (f" Valid all week (demo run {n + 1})." if n else "")
    limiter.reset(f"agent:{row['id']}")
    res = services.create_post(c, row, text)
    for r in res["notified"]:
        services.run_engagement(res["post_id"], r["agent_id"])
    p = c.execute(POST_SQL + " WHERE p.id=?", (res["post_id"],)).fetchone()
    return {"post": services.post_view(c, p), **summarize_post_result(res)}


@app.post("/api/demo/reset", dependencies=[Depends(_demo_enabled), Depends(require_admin)])
def demo_reset():
    """Wipe demo data (posts, comments, routing logs, feedback, security events, registered agents) and restore
    the six seed agents. Reload the browser afterwards."""
    return seed_reset_demo()


# ---- live events
@app.get("/api/events")
async def events():
    # no-transform: intermediaries must not re-encode/compress (and thereby buffer) the stream;
    # X-Accel-Buffering: nginx-style proxies must not buffer it. (Cloudflare in front of Render strips the latter and
    # does not buffer event-streams anyway -- measured, see DESIGN.md.)
    return StreamingResponse(hub.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ static frontend (single-service deploy)
_DIST = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return _error(404, "not_found", "No such API route.")
        f = os.path.join(_DIST, full_path)
        if full_path and os.path.isfile(f) and os.path.commonpath([os.path.abspath(f), _DIST]) == _DIST:
            return FileResponse(f)
        return FileResponse(os.path.join(_DIST, "index.html"))
