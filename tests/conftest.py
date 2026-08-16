from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pai_loop.main import create_app


@pytest.fixture()
def client() -> TestClient:
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as test_client:
        yield test_client

