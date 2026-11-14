"""Environment-based configuration. Read lazily so tests can monkeypatch env vars."""
import os


def _get(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    return _get(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


class _Settings:
    @property
    def gemini_api_key(self) -> str:
        return _get("GEMINI_API_KEY")

    @property
    def gemini_model(self) -> str:
        return _get("GEMINI_MODEL", "gemini-3.1-flash-lite")

    @property
    def gemini_embed_model(self) -> str:
        return _get("GEMINI_EMBED_MODEL", "gemini-embedding-001")

    @property
    def default_threshold(self) -> float:
        return float(_get("ROUTING_THRESHOLD", "0.6"))

    @property
    def rate_limit_per_min(self) -> int:
        return int(_get("RATE_LIMIT_PER_MIN", "10"))

    @property
    def database_path(self) -> str:
        return _get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "a2a.db"))

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in _get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]

    @property
    def admin_token(self) -> str:
        return _get("ADMIN_TOKEN")

    @property
    def enable_demo_console(self) -> bool:
        return _bool("ENABLE_DEMO_CONSOLE", True)

    @property
    def allow_private_webhooks(self) -> bool:
        return _bool("ALLOW_PRIVATE_WEBHOOKS", False)

    @property
    def gemini_api_base(self) -> str:
        return _get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")

    @property
    def gemini_max_concurrency(self) -> int:
        return max(1, min(2, int(_get("GEMINI_MAX_CONCURRENCY", "2"))))

    @property
    def gemini_min_interval(self) -> float:
        return float(_get("GEMINI_MIN_INTERVAL", "0.4"))

    @property
    def gemini_cooldown(self) -> float:
        return float(_get("GEMINI_COOLDOWN_SECONDS", "60"))

    @property
    def sse_heartbeat(self) -> float:
        return max(0.05, float(_get("SSE_HEARTBEAT_SECONDS", "5")))

    @property
    def llm_top_k(self) -> int:
        return int(_get("LLM_TOP_K", "4"))

    @property
    def force_offline(self) -> bool:
        return _bool("FORCE_OFFLINE", False)


settings = _Settings()
