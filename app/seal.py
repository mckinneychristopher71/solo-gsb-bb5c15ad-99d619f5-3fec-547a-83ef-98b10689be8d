"""报告封存：固定字段顺序的 canonical JSON 与 SHA-256 摘要。

摘要覆盖整份审计报告（批次字段 + 全部片段），时间戳统一为 UTC（`Z` 后缀）。
封存与校验复用同一份序列化逻辑，两份摘要可逐字节比较。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# 固定字段顺序：canonical JSON 的字节序以此为准，不依赖各处 dict 的自然插入顺序。
REPORT_FIELDS = (
    "id",
    "external_ref",
    "submitted_at",
    "start_time",
    "end_time",
    "low_limit_c",
    "high_limit_c",
    "hysteresis_c",
    "max_interval_seconds",
    "compliant",
    "reading_count",
    "segment_count",
    "high_excursion_count",
    "low_excursion_count",
    "missing_segment_count",
    "segments",
)

SEGMENT_FIELDS = (
    "index",
    "type",
    "start_time",
    "end_time",
    "duration_seconds",
    "sample_count",
    "peak_temperature_c",
    "peak_at",
    "max_temperature_c",
    "min_temperature_c",
    "missing",
    "compliant",
)

# 批次级时间戳字段（片段级的 start_time/end_time/peak_at 在 SEGMENT_FIELDS 中处理）。
_REPORT_TIME_FIELDS = frozenset({"submitted_at", "start_time", "end_time"})


def _utc_z(value: object) -> object:
    """datetime 统一换算到 UTC 并输出 Z 后缀；其他类型（含已是 Z 的字符串、None）原样返回。"""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    # 数据库中持久化的时间戳本身已是 UTC Z 字符串，原样保留以保证逐字节稳定。
    return value


def canonical_report_document(report: dict) -> dict:
    """按固定字段顺序构造报告文档（嵌套片段同样固定顺序），时间戳归一为 UTC Z。"""

    document: dict = {}
    for key in REPORT_FIELDS:
        if key == "segments":
            document[key] = [
                {field: _utc_z(segment[field]) for field in SEGMENT_FIELDS}
                for segment in report["segments"]
            ]
        else:
            value = report[key]
            document[key] = _utc_z(value) if key in _REPORT_TIME_FIELDS else value
    return document


def canonical_report_json(report: dict) -> bytes:
    """报告的 canonical JSON：紧凑分隔符、不转义非 ASCII、保持固定字段顺序。"""

    document = canonical_report_document(report)
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")


def report_digest(report: dict) -> str:
    """计算 canonical JSON 的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(canonical_report_json(report)).hexdigest()
