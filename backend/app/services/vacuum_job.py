"""历史清理（VACUUM）领域服务。

清理清单与规则详情读取同一份快照绝对时刻，并共用保留计划冻结的截止时刻；
执行释放前再按对象身份（快照标识）核对快照引用，无法解释 UTC 偏移的历史
版本始终留在未核查集合、不进入删除队列。同一清理作业并发/重复提交只生效
一次，中断后再次运行沿用首次确定的截止时刻且不重复回收。
"""
from __future__ import annotations

from typing import Any

from app.retention_engine import registry
from app.services.base import BaseModuleService
from app.services.retention_support import (
    load_table_snapshots,
    mark_snapshot_cleaned,
)
from app.store import store


class VacuumJobService(BaseModuleService):
    MODULE = "vacuum_job"
    ENTITY = "清理作业"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["作业标识", "目标湖表", "保留版本", "清理范围", "预览大小", "执行状态", "清理时间", "释放空间"]
    REQUIRED_FIELDS = ["作业标识", "目标湖表", "保留版本"]
    STATUS_ORDER = ["待执行", "清理中", "已完成", "清理异常"]
    ACTION_RULES = {"执行清理": "待执行", "取消清理": "清理中"}
    NEGATIVE_ACTIONS = []

    def _safe_int(self, val: Any, default: int = 0) -> int:
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def _resolve_rule(self, table: str) -> dict[str, Any] | None:
        """按目标湖表找到同表保留规则，两个入口共享同一份判定依据。"""
        for rule in store.rows("retention_policy"):
            if str(rule.get("目标湖表", "")) == str(table):
                return rule
        return None

    def _resolve_config(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, int, int]:
        """返回 (规则或None, 保留天数, 最低版本数)。

        优先使用同表保留规则；演示数据里没有配套规则的作业回退到作业
        自身的保留版本字段，保证通用清理动作仍可执行。
        """
        rule = self._resolve_rule(entry.get("目标湖表", ""))
        if rule is not None:
            retention_days = self._safe_int(rule.get("保留天数"), 30)
            min_versions = self._safe_int(
                rule.get("最低版本数"), self._safe_int(entry.get("保留版本"), 5)
            )
            return rule, retention_days, min_versions
        return None, 30, self._safe_int(entry.get("保留版本"), 5)

    def domain_preview_clean(self, entry_id: int) -> dict[str, Any]:
        """清理列表入口：与规则详情共用同一计划、同一截止时刻。"""
        entry = store.find(self.MODULE, entry_id)
        if not entry:
            return {"ok": False, "message": f"清理作业 {entry_id} 不存在"}

        rule, retention_days, min_versions = self._resolve_config(entry)
        plan_key = int(rule["id"]) if rule is not None else -entry_id
        snapshots = load_table_snapshots(entry.get("目标湖表"))
        preview = registry.preview(
            plan_key,
            snapshots,
            retention_days=retention_days,
            min_versions=min_versions,
        )
        evaluation = preview["evaluation"]
        entry["预览大小"] = evaluation["can_clean_count"]
        return {
            "ok": True,
            "job_id": entry.get("作业标识"),
            "rule_id": rule.get("规则标识") if rule else None,
            "table": entry.get("目标湖表"),
            "time_basis": preview["plan"]["time_basis"],
            "cutoff_utc": evaluation["cutoff_utc"],
            "clean_list": evaluation["deletable"],
            "unverified": evaluation["unverified"],
            "valid": evaluation["valid"],
            "unverified_count": evaluation["unverified_count"],
            "can_clean_count": evaluation["can_clean_count"],
            "plan": preview["plan"],
            "boundary_conflict": preview["boundary_conflict"],
        }

    def domain_clean_history(self, entry_id: int) -> dict[str, Any]:
        """执行清理：并发提交同一作业只生效一次，中断重跑沿用首次截止时刻。"""
        entry = store.find(self.MODULE, entry_id)
        if not entry:
            return {"ok": False, "message": f"清理作业 {entry_id} 不存在"}

        rule, retention_days, min_versions = self._resolve_config(entry)
        plan_key = int(rule["id"]) if rule is not None else -entry_id
        snapshots = load_table_snapshots(entry.get("目标湖表"))

        result = registry.execute(
            entry_id,
            plan_key,
            snapshots,
            retention_days=retention_days,
            min_versions=min_versions,
        )

        # 物理回收：回写快照保留状态，只回收本次新释放且引用核对通过的对象。
        files_cleaned = 0
        reclaimed_bytes = 0
        for released in result["released_now"]:
            if mark_snapshot_cleaned(released["snapshot_id"]):
                files_cleaned += 1
                reclaimed_bytes += self._snapshot_size_mb(released["snapshot_id"]) * 1024 * 1024

        # 释放结果回写保留策略待办：未核查对象仍保留为待处理。
        remain_unverified = sum(
            1 for snap in load_table_snapshots(entry.get("目标湖表"))
            if snap.get("status") != "已清理"
            and _is_missing_offset(snap.get("created_at"))
        )
        if rule is not None:
            rule["上次执行"] = result["cutoff_utc"]
            rule["执行结果"] = (
                f"截止时刻{result['cutoff_utc']}，累计回收{result['released_count']}条；"
                f"未核查{remain_unverified}条不进入删除队列"
            )
            rule["pending"] = remain_unverified > 0

        entry["执行状态"] = "已完成"
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["清理时间"] = result["cutoff_utc"]
        entry["释放空间"] = reclaimed_bytes // (1024 * 1024)

        return {
            "ok": True,
            "job_id": entry.get("作业标识"),
            "table": entry.get("目标湖表"),
            "retained_versions": min_versions,
            "cutoff_utc": result["cutoff_utc"],
            "runs": result["runs"],
            "idempotent": result["idempotent"],
            "files_cleaned": files_cleaned,
            "released_now": result["released_now"],
            "released_count": result["released_count"],
            "reclaimed_bytes": reclaimed_bytes,
            "clean_range": entry.get("清理范围"),
            "duration_seconds": 12,
            "unverified_remaining": remain_unverified,
        }

    @staticmethod
    def _snapshot_size_mb(snapshot_id: Any) -> int:
        for row in store.rows("snapshot_mgr"):
            if row.get("快照标识") == snapshot_id:
                try:
                    return int(row.get("存储大小", 0))
                except (ValueError, TypeError):
                    return 0
        return 0


def _is_missing_offset(created_at: Any) -> bool:
    from app.retention_engine import parse_instant

    _, evidence = parse_instant(created_at)
    return evidence == "missing-offset"
