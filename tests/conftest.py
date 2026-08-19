from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pai_loop.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    # TestClient concurrency exercises separate request-scoped SQLAlchemy
    # sessions. A :memory: engine uses StaticPool, so those sessions share one
    # DBAPI connection and one request can close/rollback it after the planner
    # lock is released while another request is committing. A file-backed
    # database gives each concurrent session its own connection, matching the
    # lifecycle isolation provided by production PostgreSQL.
    database_path = tmp_path / "pai-loop-test.db"
    app = create_app(
        database_url=f"sqlite:///{database_path.as_posix()}",
        seed_synthetic=False,
    )
    with TestClient(app) as test_client:
        yield test_client

