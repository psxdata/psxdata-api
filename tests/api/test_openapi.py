"""Unit tests for GET /openapi.json."""
import pytest
from fastapi.testclient import TestClient

import api
from api.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_openapi_version_matches_package_version(client: TestClient) -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["version"] == api.__version__
