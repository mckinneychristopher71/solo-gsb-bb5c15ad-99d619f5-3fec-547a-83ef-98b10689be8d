"""数据库读写：批次提交（写入读数与审计片段）与报告查询。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from . import audit
from .schemas import BatchCreate, to_utc


class DuplicateBatchError(Exception):
    """external_ref 已存在。"""


def _utc_iso(value: datetime) -> str:
    """以带 Z 的 ISO 8601 字符串持久化 UTC 时间。"""

    return value.isoformat().replace("+00:00", "Z")


def create_batch(conn: sqlite3.Connection, payload: BatchCreate) -> str:
    """执行审计并原子化写入批次、读数与全部片段，返回批次 ID。"""

    external_ref = payload.external_ref or uuid.uuid4().hex
    batch_id = uuid.uuid4().hex
    submitted_at = datetime.now(timezone.utc)

    readings = [
        audit.Reading(
            timestamp=to_utc(item.timestamp),
            temperature=item.temperature_c,
            source_index=index,
        )
        for index, item in enumerate(payload.readings)
    ]
    result = audit.analyze(
        readings,
        low_limit=payload.low_limit_c,
        high_limit=payload.high_limit_c,
        hysteresis=payload.hysteresis_c,
        max_interval_seconds=payload.max_interval_seconds,
    )

    try:
        with conn:
            existing = conn.execute(
                "SELECT 1 FROM batches WHERE external_ref = ?",
                (external_ref,),
            ).fetchone()
            if existing is not None:
                raise DuplicateBatchError(external_ref)

            start_time = min(r.timestamp for r in readings)
            end_time = max(r.timestamp for r in readings)

            conn.execute(
                """
                INSERT INTO batches (
                    id, external_ref, submitted_at, start_time, end_time,
                    low_limit_c, high_limit_c, hysteresis_c,
                    max_interval_seconds, compliant, reading_count,
                    high_excursion_count, low_excursion_count,
                    missing_segment_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    external_ref,
                    _utc_iso(submitted_at),
                    _utc_iso(start_time),
                    _utc_iso(end_time),
                    payload.low_limit_c,
                    payload.high_limit_c,
                    payload.hysteresis_c,
                    payload.max_interval_seconds,
                    int(result.compliant),
                    len(readings),
                    result.high_excursion_count,
                    result.low_excursion_count,
                    result.missing_segment_count,
                ),
            )

            conn.executemany(
                """
                INSERT INTO readings (batch_id, idx, timestamp, temperature_c)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        reading.source_index,
                        _utc_iso(reading.timestamp),
                        reading.temperature,
                    )
                    for reading in readings
                ],
            )

            conn.executemany(
                """
                INSERT INTO segments (
                    batch_id, idx, type, start_time, end_time,
                    duration_seconds, sample_count, peak_temperature_c,
                    peak_at, max_temperature_c, min_temperature_c,
                    missing, compliant
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        index,
                        segment.type,
                        _utc_iso(segment.start_time),
                        _utc_iso(segment.end_time),
                        segment.duration_seconds,
                        segment.sample_count,
                        segment.peak_temperature,
                        _utc_iso(segment.peak_at)
                        if segment.peak_at is not None
                        else None,
                        segment.max_temperature,
                        segment.min_temperature,
                        int(segment.missing),
                        int(segment.compliant),
                    )
                    for index, segment in enumerate(result.segments)
                ],
            )
    except sqlite3.IntegrityError as exc:
        # 并发下 UNIQUE(external_ref) 的兜底。
        raise DuplicateBatchError(external_ref) from exc

    return batch_id


def _row_to_report(row: sqlite3.Row, segments: list[sqlite3.Row]) -> dict:
    return {
        "id": row["id"],
        "external_ref": row["external_ref"],
        "submitted_at": row["submitted_at"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "low_limit_c": row["low_limit_c"],
        "high_limit_c": row["high_limit_c"],
        "hysteresis_c": row["hysteresis_c"],
        "max_interval_seconds": row["max_interval_seconds"],
        "compliant": bool(row["compliant"]),
        "reading_count": row["reading_count"],
        "segment_count": len(segments),
        "high_excursion_count": row["high_excursion_count"],
        "low_excursion_count": row["low_excursion_count"],
        "missing_segment_count": row["missing_segment_count"],
        "segments": [
            {
                "index": segment_row["idx"],
                "type": segment_row["type"],
                "start_time": segment_row["start_time"],
                "end_time": segment_row["end_time"],
                "duration_seconds": segment_row["duration_seconds"],
                "sample_count": segment_row["sample_count"],
                "peak_temperature_c": segment_row["peak_temperature_c"],
                "peak_at": segment_row["peak_at"],
                "max_temperature_c": segment_row["max_temperature_c"],
                "min_temperature_c": segment_row["min_temperature_c"],
                "missing": bool(segment_row["missing"]),
                "compliant": bool(segment_row["compliant"]),
            }
            for segment_row in segments
        ],
    }


def get_report(
    conn: sqlite3.Connection, batch_ref: str
) -> dict | None:
    """按批次 ID 或 external_ref 获取完整审计报告。"""

    row = conn.execute(
        "SELECT * FROM batches WHERE id = ? OR external_ref = ?",
        (batch_ref, batch_ref),
    ).fetchone()
    if row is None:
        return None

    segments = conn.execute(
        "SELECT * FROM segments WHERE batch_id = ? ORDER BY idx",
        (row["id"],),
    ).fetchall()
    return _row_to_report(row, segments)


def list_batches(
    conn: sqlite3.Connection,
    *,
    compliant: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    where = ""
    params: list[object] = []
    if compliant is not None:
        where = "WHERE compliant = ?"
        params.append(int(compliant))

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM batches {where}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""
        SELECT * FROM batches {where}
        ORDER BY submitted_at DESC, id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    items = [
        {
            "id": row["id"],
            "external_ref": row["external_ref"],
            "submitted_at": row["submitted_at"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "compliant": bool(row["compliant"]),
            "reading_count": row["reading_count"],
            "high_excursion_count": row["high_excursion_count"],
            "low_excursion_count": row["low_excursion_count"],
            "missing_segment_count": row["missing_segment_count"],
        }
        for row in rows
    ]
    return items, total
