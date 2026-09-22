"""API 端到端测试：使用临时 SQLite 文件，互不影响开发库。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("COLDCHAIN_DB_PATH", str(db_path))

    # 必须在设置环境变量后再导入，保证配置生效。
    import app.config as config
    import app.database as database
    from app.main import app

    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    with TestClient(app) as test_client:
        yield test_client


def _payload(**overrides):
    payload = {
        "external_ref": "shipment-001",
        "low_limit": 2.0,
        "high_limit": 8.0,
        "hysteresis": 0.5,
        "max_interval_seconds": 900,
        "readings": [
            {"timestamp": "2026-09-22T08:00:00Z", "temperature": 5.0},
            {"timestamp": "2026-09-22T08:10:00Z", "temperature": 5.5},
            {"timestamp": "2026-09-22T08:20:00Z", "temperature": 4.5},
        ],
    }
    payload.update(overrides)
    return payload


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_submit_compliant_batch_and_fetch_report(client: TestClient) -> None:
    response = client.post("/batches", json=_payload())
    assert response.status_code == 201, response.text
    report = response.json()

    assert report["external_ref"] == "shipment-001"
    assert report["compliant"] is True
    assert report["reading_count"] == 3
    assert len(report["segments"]) == 1
    segment = report["segments"][0]
    assert segment["type"] == "normal"
    assert segment["compliant"] is True
    assert segment["missing"] is False
    assert segment["start_time"] == "2026-09-22T08:00:00Z"

    # 按 external_ref 查询报告。
    fetched = client.get("/batches/shipment-001/report")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == report["id"]


def test_excursion_and_missing_segments_marked_non_compliant(
    client: TestClient,
) -> None:
    payload = _payload(
        external_ref="shipment-002",
        readings=[
            {"timestamp": "2026-09-22T08:00:00Z", "temperature": 9.2},
            {"timestamp": "2026-09-22T08:05:00Z", "temperature": 9.4},
            {"timestamp": "2026-09-22T09:00:00Z", "temperature": 5.0},
        ],
    )
    response = client.post("/batches", json=payload)
    assert response.status_code == 201, response.text
    report = response.json()

    assert report["compliant"] is False
    assert report["high_excursion_count"] == 1
    assert report["missing_segment_count"] == 1

    types = [s["type"] for s in report["segments"]]
    assert types == ["high_excursion", "missing", "normal"]

    excursion = report["segments"][0]
    assert excursion["peak_temperature_c"] == 9.4
    assert excursion["peak_at"] == "2026-09-22T08:05:00Z"

    missing = report["segments"][1]
    assert missing["missing"] is True
    assert missing["compliant"] is False
    assert missing["sample_count"] == 0


def test_duplicate_external_ref_returns_409(client: TestClient) -> None:
    response = client.post("/batches", json=_payload())
    assert response.status_code == 201
    duplicate = client.post("/batches", json=_payload())
    assert duplicate.status_code == 409


def test_report_not_found(client: TestClient) -> None:
    response = client.get("/batches/unknown/report")
    assert response.status_code == 404


def test_invalid_limits_return_422(client: TestClient) -> None:
    response = client.post(
        "/batches", json=_payload(low_limit=9.0, high_limit=2.0)
    )
    assert response.status_code == 422


def test_list_batches_and_filter(client: TestClient) -> None:
    client.post("/batches", json=_payload())
    client.post(
        "/batches",
        json=_payload(
            external_ref="shipment-bad",
            readings=[
                {"timestamp": "2026-09-22T08:00:00Z", "temperature": 5.0},
                {"timestamp": "2026-09-22T08:05:00Z", "temperature": 12.0},
            ],
        ),
    )

    all_batches = client.get("/batches")
    assert all_batches.status_code == 200
    assert all_batches.json()["total"] == 2

    compliant = client.get("/batches", params={"compliant": "true"})
    items = compliant.json()["items"]
    assert len(items) == 1
    assert items[0]["external_ref"] == "shipment-001"


def test_unsorted_submission_is_sorted_server_side(client: TestClient) -> None:
    payload = _payload(
        external_ref="unsorted",
        readings=[
            {"timestamp": "2026-09-22T08:20:00Z", "temperature": 5.0},
            {"timestamp": "2026-09-22T08:00:00Z", "temperature": 9.0},
            {"timestamp": "2026-09-22T08:10:00Z", "temperature": 9.5},
        ],
    )
    response = client.post("/batches", json=payload)
    assert response.status_code == 201
    segments = response.json()["segments"]
    assert segments[0]["type"] == "high_excursion"
    assert segments[0]["start_time"] == "2026-09-22T08:00:00Z"
    assert segments[0]["peak_temperature_c"] == 9.5


def test_seal_is_idempotent_and_verify_matches(client: TestClient) -> None:
    created = client.post("/batches", json=_payload())
    assert created.status_code == 201

    sealed = client.post("/batches/shipment-001/seal")
    assert sealed.status_code == 200, sealed.text
    body = sealed.json()
    assert body["sealed"] is True
    assert body["sealed_digest"] == body["digest"]
    first_sealed_at = body["sealed_at"]
    assert first_sealed_at.endswith("Z") or "+00:00" in first_sealed_at

    # 重复封存必须返回完全一致的结果。
    resealed = client.post("/batches/shipment-001/seal")
    assert resealed.status_code == 200
    assert resealed.json() == body

    # 未被篡改时校验通过。
    verification = client.get("/batches/shipment-001/verify")
    assert verification.status_code == 200
    verify_body = verification.json()
    assert verify_body["valid"] is True
    assert verify_body["digest"] == body["digest"]
    assert verify_body["sealed_digest"] == body["digest"]
    assert verify_body["sealed_at"] == first_sealed_at


def test_verify_reports_invalid_after_tampering_without_mutating_data(
    client: TestClient, monkeypatch
) -> None:
    import app.config as config

    created = client.post("/batches", json=_payload())
    batch_id = created.json()["id"]

    sealed = client.post("/batches/shipment-001/seal")
    sealed_digest = sealed.json()["sealed_digest"]

    # 绕过 API 直接篡改底层审计数据，模拟封存后数据被改动。
    import sqlite3

    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "UPDATE segments SET max_temperature_c = 99.5 WHERE batch_id = ?",
            (batch_id,),
        )
        conn.commit()
    finally:
        conn.close()

    verification = client.get("/batches/shipment-001/verify")
    assert verification.status_code == 200
    body = verification.json()
    assert body["valid"] is False
    assert body["sealed_digest"] == sealed_digest
    assert body["digest"] != sealed_digest

    # 校验不得回写审计数据：篡改值仍然存在，且再次封存不覆盖原摘要。
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT max_temperature_c FROM segments WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        assert row[0] == 99.5
        row = conn.execute(
            "SELECT sealed_digest FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        assert row[0] == sealed_digest
    finally:
        conn.close()

    resealed = client.post("/batches/shipment-001/seal")
    assert resealed.json()["sealed_digest"] == sealed_digest


def test_seal_and_verify_unknown_batch_return_404(client: TestClient) -> None:
    assert client.post("/batches/unknown/seal").status_code == 404
    assert client.get("/batches/unknown/verify").status_code == 404


def test_verify_unsealed_batch_is_invalid(client: TestClient) -> None:
    client.post("/batches", json=_payload())
    response = client.get("/batches/shipment-001/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["sealed_digest"] is None
    assert body["sealed_at"] is None
    assert len(body["digest"]) == 64
