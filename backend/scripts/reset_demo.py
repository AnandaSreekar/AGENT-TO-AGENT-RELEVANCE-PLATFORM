"""Wipe demo data so you can record from a clean state (keeps the six seed agents and their keys).

    cd backend
    python scripts/reset_demo.py            # uses DATABASE_PATH (default backend/data/a2a.db)

Removes posts, comments, routing logs, feedback stats, security events, webhook log, registered (non-seed)
agents and the Attack Lab bot; restores seed agents' trust / autonomy / subscriptions and the default threshold.

If the server is running, prefer the endpoint (it also clears the server's in-memory rate limits):
    curl -X POST http://127.0.0.1:8000/api/demo/reset            (add -H "X-Admin-Token: ..." if ADMIN_TOKEN is set)
Reload the browser afterwards.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.seed import reset_demo, seed_if_empty  # noqa: E402

path = os.path.abspath(settings.database_path)
init_db()
seed_if_empty()
r = reset_demo()
print(f"Reset {path}")
print(f"  removed: {r['removed']}  extra agents: {r['unregistered_agents_removed']}")
print("  six seed agents restored. If a server is running, restart it or call POST /api/demo/reset to clear in-memory rate limits.")
