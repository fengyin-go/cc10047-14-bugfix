"""快照保留治理路由。

把保留策略规则详情、清理作业清理列表、时间证据补正和清理执行收敛为一组
显式端点。规则详情与清理列表背后读取同一份快照绝对时刻、共用同一个保留
计划，因此两个入口对同一瞬间（含 +14:00 / -12:00 等价时刻）的判定一致。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.retention_engine import parse_instant, registry, to_iso_utc
from app.services import service_for
from app.services.retention_support import (
    load_table_snapshots,
    repair_snapshot_offset,
)
from app.store import store

router = APIRouter(prefix="/api/retention", tags=["快照保留治理"])


class EvidenceRepairPayload(BaseModel):
    snapshot_id: str
    created_at: str


@router.get("/rules/{rule_id}/preview")
def preview_rule(rule_id: int) -> dict:
    """规则详情入口：按保留天数预览保留判定，冻结截止时刻。"""
    svc = service_for("retention_policy")
    result = svc.preview_plan(rule_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.get("/jobs/{job_id}/clean-list")
def clean_list(job_id: int) -> dict:
    """清理列表入口：与规则详情共用同一计划与截止时刻。"""
    svc = service_for("vacuum_job")
    result = svc.domain_preview_clean(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/evidence/repair")
def repair_evidence(payload: EvidenceRepairPayload) -> dict:
    """补正一条缺少偏移的历史快照时间证据。

    补正后重新评估关联规则；若新旧边界产生不同释放集合，返回的
    ``boundary_conflict`` 会标出采用的时间依据与冲突差异，未核查对象
    仍单独列出且不会进入删除队列。
    """
    try:
        row = repair_snapshot_offset(payload.snapshot_id, payload.created_at)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    table = row.get("目标湖表")
    rule = next(
        (item for item in store.rows("retention_policy")
         if str(item.get("目标湖表", "")) == str(table)),
        None,
    )
    conflict = None
    evaluation = None
    if rule is not None:
        svc = service_for("retention_policy")
        result = svc.preview_plan(int(rule["id"]))
        conflict = result.get("boundary_conflict")
        evaluation = result
    return {
        "ok": True,
        "message": f"快照 {payload.snapshot_id} 时间证据已补正",
        "snapshot_id": payload.snapshot_id,
        "created_at": payload.created_at,
        "instant_utc": to_iso_utc(parse_instant(payload.created_at)[0]),
        "boundary_conflict": conflict,
        "evaluation": evaluation,
        "unverified_snapshots": [
            item for item in load_table_snapshots(table)
            if parse_instant(item.get("created_at"))[1] != "offset"
        ],
    }


@router.post("/jobs/{job_id}/execute")
def execute_clean(job_id: int) -> dict:
    """执行清理：并发/重复提交只生效一次，中断重跑沿用首次截止时刻。"""
    svc = service_for("vacuum_job")
    result = svc.domain_clean_history(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@router.get("/plans/{rule_id}")
def get_plan(rule_id: int) -> dict:
    """查看规则当前保留计划（截止时刻、边界冲突、采用的时间依据）。"""
    plan = registry.get_plan(rule_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id} 尚未生成保留计划")
    return plan
