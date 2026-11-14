"""SSE stream behaviour: small chunks, prompt heartbeat, prompt delivery from worker threads, response headers."""
import asyncio
import threading
import time

from app.events import Hub


def test_heartbeat_and_prompt_delivery(monkeypatch):
    monkeypatch.setenv("SSE_HEARTBEAT_SECONDS", "0.1")

    async def go():
        h = Hub()
        h.attach(asyncio.get_running_loop())
        gen = h.stream()
        assert (await gen.__anext__()).startswith("retry:")
        assert (await gen.__anext__()).startswith(": stream open")
        t0 = time.monotonic()
        assert await gen.__anext__() == ": keep-alive\n\n"          # idle -> heartbeat on schedule
        assert 0.05 <= time.monotonic() - t0 < 0.6
        # events published from a worker thread (as the sync endpoints do) are delivered immediately and in order
        sent = {}

        def publish():
            for i in range(3):
                sent[i] = time.monotonic()
                h.publish("notified", {"n": i})

        threading.Thread(target=publish).start()
        got = []
        while len(got) < 3:
            chunk = await asyncio.wait_for(gen.__anext__(), 2)
            if chunk.startswith("event: notified"):
                got.append((time.monotonic(), chunk))
        assert [c.count('"n": %d' % i) for i, (_, c) in enumerate(got)] == [1, 1, 1]  # order preserved
        assert max(t - sent[i] for i, (t, _) in enumerate(got)) < 0.5                  # no batching delay
        assert all(c.endswith("\n\n") for _, c in got)                                 # one complete SSE frame per chunk
        await gen.aclose()
        assert not h.queues                                                            # subscriber cleaned up

    asyncio.run(go())


def test_default_heartbeat_is_a_few_seconds(monkeypatch):
    from app.config import settings
    monkeypatch.delenv("SSE_HEARTBEAT_SECONDS", raising=False)
    assert settings.sse_heartbeat == 5.0
    monkeypatch.setenv("SSE_HEARTBEAT_SECONDS", "0")
    assert settings.sse_heartbeat == 0.05  # clamped, never a busy loop


def test_sse_response_headers():
    from app.main import events
    resp = asyncio.run(events())
    assert resp.media_type == "text/event-stream"
    cc = resp.headers["cache-control"]
    assert "no-cache" in cc and "no-transform" in cc
    assert resp.headers["x-accel-buffering"] == "no"
    assert "content-encoding" not in resp.headers  # never compressed
