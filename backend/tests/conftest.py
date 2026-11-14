import os
import sys
import tempfile

# Must be set before the app is imported. Tests always run in Demo Mode (no network).
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(prefix="a2a-test-"), "test.db")
os.environ["FORCE_OFFLINE"] = "true"
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("ADMIN_TOKEN", None)
os.environ["RATE_LIMIT_PER_MIN"] = "10"
os.environ["ENABLE_DEMO_CONSOLE"] = "true"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.safety import limiter  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield


@pytest.fixture(scope="session")
def keys(client):
    r = client.get("/api/demo/keys").json()["keys"]
    return {k["company_name"]: k["api_key"] for k in r}


@pytest.fixture(scope="session")
def agents(client):
    return {a["company_name"]: a for a in client.get("/api/agents").json()["agents"]}


def H(key):
    return {"X-API-Key": key}
