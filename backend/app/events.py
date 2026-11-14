"""Live event hub (SSE) and HMAC-signed webhook delivery."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import time
from urllib.parse import urlparse

import httpx

from .config import settings
from .db import db, now
from .safety import validate_webhook_url


class Hub:
    def __init__(self):
        self.queues: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def attach(self, loop):
        self.loop = loop

    def publish(self, type_: str, data: dict):
        evt = {"type": type_, "data": data, "ts": now()}
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._fanout, evt)

    def _fanout(self, evt):
        for q in list(self.queues):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass  # slow consumer: drop rather than block publishers

    async def stream(self):
        """SSE generator. Every yield is one small chunk; the server never batches events itself.
        A comment line is sent every SSE_HEARTBEAT_SECONDS (default 5 s) so idle connections stay open through
        proxies and a dead client is noticed quickly."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.queues.add(q)
        try:
            yield "retry: 3000\n\n"
            yield ": stream open\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=settings.sse_heartbeat)
                    yield f"event: {evt['type']}\ndata: {json.dumps(evt)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            self.queues.discard(q)


hub = Hub()


# ---------------------------------------------------------------- webhooks
def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def _resolves_private(host: str) -> bool:
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except OSError:
        return True  # unresolvable: treat as unsafe
    return False


def deliver_webhook(agent_id: int, post_id: int, payload: dict) -> None:
    """Best-effort signed POST with 3 attempts. Never raises; outcome is recorded."""
    try:
        with db() as c:
            a = c.execute("SELECT webhook_url, webhook_secret FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not a or not a["webhook_url"]:
            return
        url, secret = a["webhook_url"], a["webhook_secret"] or ""
        err = validate_webhook_url(url)
        if not err and not settings.allow_private_webhooks and _resolves_private(urlparse(url).hostname or ""):
            err = "webhook host resolves to a private address"
        attempts, status = 0, "FAILED"
        if not err:
            body = json.dumps(payload, separators=(",", ":")).encode()
            for attempts in (1, 2, 3):
                ts = str(int(time.time()))
                try:
                    r = httpx.post(url, content=body, timeout=3.0, follow_redirects=False, headers={
                        "content-type": "application/json", "x-a2a-timestamp": ts,
                        "x-a2a-signature": "sha256=" + sign(secret, ts, body)})
                    if 200 <= r.status_code < 300:
                        status, err = "DELIVERED", None
                        break
                    err = f"HTTP {r.status_code}"
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}"
                time.sleep(0.4 * attempts)
        with db() as c:
            c.execute("INSERT INTO webhook_deliveries(post_id,agent_id,status,attempts,error,created_at) VALUES(?,?,?,?,?,?)",
                      (post_id, agent_id, status, attempts, err, now()))
    except Exception:  # noqa: BLE001 - delivery must never crash the request lifecycle
        pass
