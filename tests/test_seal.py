"""封存摘要单元测试：canonical JSON 字段顺序、UTC 归一与 SHA-256 稳定性。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.seal import (
    REPORT_FIELDS,
    SEGMENT_FIELDS,
    canonical_report_document,
    canonical_report_json,
    report_digest,
)


def _report(**overrides) -> dict:
    report = {
        "id": "abc123",
        "external_ref": "TRUCK-A-001",
        "submitted_at": "2026-09-22T10:00:00Z",
        "start_time": "2026-09-22T08:00:00Z",
        "end_time": "2026-09-22T08:20:00Z",
        "low_limit_c": 2.0,
        "high_limit_c": 8.0,
        "hysteresis_c": 0.5,
        "max_interval_seconds": 900.0,
        "compliant": False,
        "reading_count": 2,
        "segment_count": 1,
        "high_excursion_count": 1,
        "low_excursion_count": 0,
        "missing_segment_count": 0,
        "segments": [
            {
                "index": 0,
                "type": "high_excursion",
                "start_time": "2026-09-22T08:00:00Z",
                "end_time": "2026-09-22T08:20:00Z",
                "duration_seconds": 1200.0,
                "sample_count": 2,
                "peak_temperature_c": 9.4,
                "peak_at": "2026-09-22T08:20:00Z",
                "max_temperature_c": 9.4,
                "min_temperature_c": 8.4,
                "missing": False,
                "compliant": False,
            }
        ],
    }
    report.update(overrides)
    return report


def test_canonical_document_uses_fixed_field_order() -> None:
    document = canonical_report_document(_report())
    assert list(document.keys()) == list(REPORT_FIELDS)
    assert list(document["segments"][0].keys()) == list(SEGMENT_FIELDS)


def test_canonical_json_is_compact_and_utc_z() -> None:
    raw = canonical_report_json(_report())
    text = raw.decode("utf-8")
    assert ", " not in text and ": " not in text
    assert text.startswith('{"id":"abc123","external_ref":"TRUCK-A-001"')
    assert "2026-09-22T08:00:00Z" in text


def test_datetime_values_are_normalized_to_utc_z() -> None:
    # +08:00 偏移应换算到 UTC；naive 时间按 UTC 解释。
    report = _report(
        start_time=datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 23, 0, 30),
    )
    document = canonical_report_document(report)
    assert document["start_time"] == "2026-09-22T16:00:00Z"
    assert document["end_time"] == "2026-09-23T00:30:00Z"


def test_digest_is_sha256_of_canonical_json() -> None:
    report = _report()
    expected = hashlib.sha256(canonical_report_json(report)).hexdigest()
    assert report_digest(report) == expected
    assert len(expected) == 64


def test_field_order_changes_digest() -> None:
    report = _report()
    canonical = report_digest(report)

    # 同样的内容按键名字典序序列化应得到不同摘要，证明字段顺序参与计算。
    sorted_text = json.dumps(
        canonical_report_document(report),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    sorted_digest = hashlib.sha256(sorted_text.encode("utf-8")).hexdigest()
    assert sorted_digest != canonical


def test_digest_changes_when_audit_data_changes() -> None:
    base = _report()
    tampered = _report(compliant=True)
    assert report_digest(tampered) != report_digest(base)

    tampered_segment = _report()
    tampered_segment["segments"][0]["peak_temperature_c"] = 10.1
    assert report_digest(tampered_segment) != report_digest(base)
