"""保留策略领域服务。

规则详情与清理列表通过 ``retention_support.load_table_snapshots`` 读取同一份
快照时间证据，时区换算统一交给 ``retention_engine``，因此两个入口对同一
绝对时刻（含 ``+14:00`` 与 ``-12:00`` 等价时刻）的保留判定必然一致。
"""
from __future__ import annotations

from typing import Any

from app.retention_engine import registry
from app.services.base import BaseModuleService
from app.services.retention_support import load_table_snapshots
from app.store import store


class RetentionPolicyService(BaseModuleService):
    MODULE = "retention_policy"
    ENTITY = "保留规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则标识", "目标湖表", "保留天数", "清理方式", "豁免标记", "上次执行", "执行结果", "下次执行"]
    REQUIRED_FIELDS = ["规则标识", "目标湖表", "保留天数"]
    STATUS_ORDER = ["待生效", "已生效", "已暂停", "已过期"]
    ACTION_RULES = {"启用规则": "待生效", "暂停规则": "已生效"}
    NEGATIVE_ACTIONS = []

    def _safe_int(self, val: Any, default: int = 0) -> int:
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def _rule_config(self, entry: dict[str, Any]) -> tuple[int, int]:
        """保留天数与最低版本数作为初始门槛读取。"""
        retention_days = self._safe_int(entry.get("保留天数"), 30)
        min_versions = self._safe_int(entry.get("最低版本数"), 5)
        return retention_days, min_versions

    def domain_check_retention(self, entry_id: int) -> dict[str, Any]:
        """规则详情入口：对同一份快照绝对时刻执行保留判定（预览）。

        处理流程：读取规则配置 → 装配与清理列表同源的快照集合 →
        冻结/沿用截止时刻 → 逐条保留判定 → 回写规则待办结论。
        """
        entry = store.find(self.MODULE, entry_id)
        if not entry:
            return {"ok": False, "message": f"保留规则 {entry_id} 不存在"}

        retention_days, min_versions = self._rule_config(entry)
        snapshots = load_table_snapshots(entry.get("目标湖表"))
        preview = registry.preview(
            entry_id,
            snapshots,
            retention_days=retention_days,
            min_versions=min_versions,
        )
        evaluation = preview["evaluation"]
        result = {
            "ok": True,
            "rule_id": entry.get("规则标识"),
            "table": entry.get("目标湖表"),
            "time_basis": preview["plan"]["time_basis"],
            "cutoff_utc": evaluation["cutoff_utc"],
            "retention_days": evaluation["retention_days"],
            "min_versions_required": evaluation["min_versions_required"],
            "total_snapshots": evaluation["total_snapshots"],
            "expired_count": evaluation["expired_count"],
            "valid_count": evaluation["valid_count"],
            "unverified_count": evaluation["unverified_count"],
            "protected_count": evaluation["protected_count"],
            "can_clean_count": evaluation["can_clean_count"],
            "should_clean": evaluation["should_clean"],
            "snapshots": evaluation["snapshots"],
            "deletable": evaluation["deletable"],
            "unverified": evaluation["unverified"],
            "plan": preview["plan"],
            "boundary_conflict": preview["boundary_conflict"],
        }
        # 核对结论落在快照保留判定结果上，并回写保留策略待办。
        entry["执行结果"] = self._summarize_result(evaluation)
        entry["pending"] = evaluation["unverified_count"] > 0 or evaluation["should_clean"]
        return result

    def preview_plan(self, entry_id: int) -> dict[str, Any]:
        """显式预览接口：语义与规则详情一致，返回完整计划与逐条判定。"""
        return self.domain_check_retention(entry_id)

    @staticmethod
    def _summarize_result(evaluation: dict[str, Any]) -> str:
        return (
            f"截止时刻{evaluation['cutoff_utc']}："
            f"可清理{evaluation['can_clean_count']}条，"
            f"有效{evaluation['valid_count']}条，"
            f"最低版本保护{evaluation['protected_count']}条，"
            f"未核查{evaluation['unverified_count']}条"
        )
