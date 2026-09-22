"""骨架冒烟测试。"""
from fastapi.testclient import TestClient

from main import app


def test_root_ok() -> None:
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
