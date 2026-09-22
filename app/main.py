"""冷链温度合规审计 API（FastAPI + SQLite）。

启动时自动建库建表，默认零配置、无需第三方账号。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from . import crud
from .database import get_db, init_db
from .schemas import (
    BatchCreate,
    BatchList,
    BatchReport,
    BatchSeal,
    BatchSummary,
    SealVerification,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    # 依赖与数据库初始化：目录创建 + 幂等建表。
    init_db()
    yield


app = FastAPI(
    title="冷链温度合规审计 API",
    version="1.1.0",
    description=(
        "提交按批次组织的 ISO 时间戳温度读数，服务端排序后使用滞回阈值"
        "识别连续超温事件，并将超过采样间隔上限的缺测切成独立片段；"
        "支持对审计报告按固定字段顺序生成 canonical JSON 并以 SHA-256"
        "封存摘要（seal），随后可重新计算摘要进行一致性校验（verify）。"
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/batches",
    response_model=BatchReport,
    status_code=status.HTTP_201_CREATED,
    tags=["batches"],
    summary="提交一批温度读数并完成审计",
)
def submit_batch(
    payload: BatchCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    try:
        batch_id = crud.create_batch(conn, payload)
    except crud.DuplicateBatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"batch with external_ref already exists: {exc}",
        )

    report = crud.get_report(conn, batch_id)
    assert report is not None  # 刚写入，必然存在
    return report


@app.get(
    "/batches",
    response_model=BatchList,
    tags=["batches"],
    summary="列出批次摘要",
)
def list_batches(
    compliant: bool | None = Query(
        default=None, description="按是否合规过滤"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    items, total = crud.list_batches(
        conn, compliant=compliant, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get(
    "/batches/{batch_ref}/report",
    response_model=BatchReport,
    tags=["batches"],
    summary="查询批次审计报告（批次 ID 或业务批次号）",
)
def get_batch_report(
    batch_ref: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    report = crud.get_report(conn, batch_ref)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {batch_ref}",
        )
    return report


@app.post(
    "/batches/{batch_ref}/seal",
    response_model=BatchSeal,
    tags=["sealing"],
    summary="封存批次报告摘要（canonical JSON + SHA-256）",
)
def seal_batch(
    batch_ref: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    seal = crud.seal_report(conn, batch_ref)
    if seal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {batch_ref}",
        )
    return seal


@app.get(
    "/batches/{batch_ref}/verify",
    response_model=SealVerification,
    tags=["sealing"],
    summary="校验当前报告摘要与封存摘要是否一致（只读）",
)
def verify_batch(
    batch_ref: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    verification = crud.get_verification(conn, batch_ref)
    if verification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch not found: {batch_ref}",
        )
    return verification
