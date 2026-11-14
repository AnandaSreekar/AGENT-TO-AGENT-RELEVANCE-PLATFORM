"""Free-tier friendliness of the Gemini client + honesty of live/demo labelling. Gemini is mocked at the HTTP layer
(httpx.post): these tests do NOT prove anything about the real Gemini API."""
import copy
import threading
import time

import httpx
import pytest

from app import evaluation, llm
from app.config import settings


class FakeResp:
    def __init__(self, status=200, json_body=None, headers=None):
        self.status_code, self._j, self.headers = status, json_body or {}, headers or {}

    def json(self):
        return self._j

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"{self.status_code}", request=httpx.Request("POST", "http://x"), response=self)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def live_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(settings.__class__, "force_offline", property(lambda s: False))
    monkeypatch.setitem(llm._state, "disabled_until", 0.0)
    monkeypatch.setitem(llm._state, "failures", 0)
    llm._next_call[0] = 0.0
    yield
    llm._state.update(disabled_until=0.0, failures=0)
    evaluation._cache.pop("live", None)
    evaluation.progress.update(state="idle", done=0, error=None, message="")


def test_at_most_two_requests_in_flight(monkeypatch):
    monkeypatch.setenv("GEMINI_MIN_INTERVAL", "0")
    inflight, peak, lock = [0], [0], threading.Lock()

    def fake_post(*a, **k):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.05)
        with lock:
            inflight[0] -= 1
        return FakeResp(200, {"ok": 1})

    monkeypatch.setattr(httpx, "post", fake_post)
    ts = [threading.Thread(target=llm._post, args=("models/x:generateContent", {})) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] <= 2

    monkeypatch.setenv("GEMINI_MAX_CONCURRENCY", "1")
    peak[0] = 0
    ts = [threading.Thread(target=llm._post, args=("models/x:generateContent", {})) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1


def test_requests_are_spaced(monkeypatch):
    monkeypatch.setenv("GEMINI_MIN_INTERVAL", "0.15")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp(200, {}))
    t0 = time.monotonic()
    for _ in range(4):
        llm._post("models/x:generateContent", {})
    assert time.monotonic() - t0 >= 0.40  # 3 gaps x 0.15s


def test_retry_after_is_honoured(monkeypatch):
    monkeypatch.setenv("GEMINI_MIN_INTERVAL", "0")
    calls = []

    def fake_post(*a, **k):
        calls.append(time.monotonic())
        return FakeResp(429, {}, {"retry-after": "0.4"}) if len(calls) == 1 else FakeResp(200, {"ok": 1})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert llm._post("models/x:generateContent", {}) == {"ok": 1}
    assert len(calls) == 2 and calls[1] - calls[0] >= 0.38
    assert llm.live_available()  # a short, honoured 429 does not open the breaker


def test_google_retryinfo_body_is_parsed():
    body = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "27s"}]}}
    assert llm._retry_after(FakeResp(429, body)) == 27.0


def test_long_retry_after_opens_breaker_without_sleeping(monkeypatch):
    monkeypatch.setenv("GEMINI_MIN_INTERVAL", "0")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp(429, {}, {"retry-after": "90"}))
    t0 = time.monotonic()
    with pytest.raises(httpx.HTTPStatusError):
        llm._post("models/x:generateContent", {})
    assert time.monotonic() - t0 < 2
    assert not llm.live_available()
    assert llm.cooldown_remaining() >= 85  # honours the server's 90s, not just our 60s default
    info = llm.mode_info()
    assert info["mode"] == "demo" and info["label"] == "Demo Mode" and info["degraded"] is True


def test_three_failures_open_breaker_for_about_60s(monkeypatch):
    for _ in range(3):
        llm._fail(RuntimeError("boom"))
    assert not llm.live_available()
    assert 55 <= llm.cooldown_remaining() <= 60
    # while open, judge() answers offline and says so
    res = llm.judge({"name": "PhonePe", "industry": "Payments", "manifesto": "UPI payments", "interests": ["payments"]},
                    {"name": "Swiggy", "industry": "Food"}, "UPI cashback offer on payments", ["payments"])
    assert res["engine"] == "offline-rules"


# ---------------------------------------------------------------- provenance: never label non-live numbers as live
def _fake_live_posts(offline_records=0):
    posts = copy.deepcopy(evaluation._demo_scores()["posts"])
    n = 0
    for p in posts:
        p["semantic_engine"] = "gemini-embeddings"
        for s in p["scores"].values():
            if s["judged"]:
                s["engine"] = "offline-rules" if n < offline_records else "gemini"
                n += 1
    return posts


def test_provenance_labels():
    assert evaluation._demo_scores()["provenance"]["mode"] == "demo"
    assert evaluation._provenance(_fake_live_posts(0))["mode"] == "live"
    # a post filtered out by the rules never runs stage 2: it must not make a fully-live run look "mixed"
    posts = _fake_live_posts(0)
    posts[1]["semantic_engine"] = None
    assert evaluation._provenance(posts)["mode"] == "live"
    mixed = evaluation._provenance(_fake_live_posts(1))
    assert mixed["mode"] == "mixed" and mixed["judge_calls_offline"] == 1


def _wait_done(timeout=10):
    t0 = time.time()
    while evaluation.progress["state"] == "running" and time.time() - t0 < timeout:
        time.sleep(0.05)


def test_rescore_runs_sequentially_and_labels_live(monkeypatch):
    monkeypatch.setattr(evaluation, "EVAL_PAUSE", 0)
    seen_workers = []
    base = _fake_live_posts(0)
    posts_by_idx = {p["post_idx"]: p for p in base}

    def fake_score_one(profiles, by_name, i, author, content, offline_only, workers=None):
        seen_workers.append((offline_only, workers))
        return posts_by_idx[i]

    monkeypatch.setattr(evaluation, "_score_one", fake_score_one)
    monkeypatch.setattr(llm, "live_available", lambda: True)
    evaluation.start_rescore()
    _wait_done()
    assert evaluation.progress["state"] == "done" and evaluation.progress["done"] == 15
    assert all(w == (False, 1) for w in seen_workers) and len(seen_workers) == 15  # one call at a time
    assert evaluation.run_eval(0.6)["mode"] == "live"


def test_rescore_aborts_and_discards_when_gemini_drops(monkeypatch):
    monkeypatch.setattr(evaluation, "EVAL_PAUSE", 0)
    base = {p["post_idx"]: p for p in _fake_live_posts(0)}
    state = {"n": 0, "up": True}

    def fake_score_one(profiles, by_name, i, author, content, offline_only, workers=None):
        state["n"] += 1
        if state["n"] == 4:
            state["up"] = False  # Gemini goes away after 4 posts
        return base[i]

    monkeypatch.setattr(evaluation, "_score_one", fake_score_one)
    monkeypatch.setattr(llm, "live_available", lambda: state["up"])
    evaluation.start_rescore()
    _wait_done()
    assert evaluation.progress["state"] == "failed" and "discarded" in evaluation.progress["error"]
    assert "live" not in evaluation._cache            # partial data never saved as live
    assert evaluation.run_eval(0.6)["mode"] == "demo"  # UI keeps showing honestly-labelled demo numbers


def test_rescore_all_fallbacks_is_not_saved_as_live(monkeypatch):
    monkeypatch.setattr(evaluation, "EVAL_PAUSE", 0)
    demo = {p["post_idx"]: p for p in copy.deepcopy(evaluation._demo_scores()["posts"])}
    monkeypatch.setattr(evaluation, "_score_one", lambda p, b, i, a, c, o, workers=None: demo[i])  # all offline
    monkeypatch.setattr(llm, "live_available", lambda: True)
    evaluation.start_rescore()
    _wait_done()
    assert evaluation.progress["state"] == "failed" and "Not saved as live" in evaluation.progress["error"]
    assert "live" not in evaluation._cache


def test_rescore_endpoint_without_key_is_409(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = client.post("/api/eval/rescore")
    assert r.status_code == 409 and r.json()["error"]["code"] == "live_unavailable"
    assert "GEMINI_API_KEY" in r.json()["error"]["message"]
    assert client.get("/api/eval/progress").json()["state"] == "idle"
