"""快照保留判定引擎。

核心约定
========
1. 所有带时区的快照时间统一换算为 UTC 绝对时刻（``datetime``，tzinfo=utc），
   规则详情、清理清单、执行释放读取的是同一份绝对时刻，因此 ``+14:00`` 与
   ``-12:00`` 表示同一瞬间的两个字符串必然得到完全相同的保留判定。
2. 保留天数（``retention_days``）与最低版本数（``min_versions``）只作为初始
   门槛：先按截止时刻判定是否过期，再从过期集合里为最新的 ``min_versions``
   个版本保留豁免。
3. 无法解释 UTC 偏移的历史快照（时间字符串缺失偏移）标记为
   ``unverified``，不会进入删除队列，必须先补正时间证据。
4. 每次预览冻结一个截止时刻（cutoff）；同一份计划在中断后重新执行仍沿用首次
   确定的截止时刻，边界冲突时以可解释的带偏移绝对时刻为准。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any


# ------------------------------------------------------------------
# 时间解析：带偏移 -> UTC 绝对时刻；缺偏移 -> 不解释、不猜测
# ------------------------------------------------------------------


def parse_instant(value: Any) -> tuple[datetime | None, str]:
    """把快照时间字符串解析为 UTC 绝对时刻。

    返回 ``(instant, evidence)``：
    - ``evidence == "offset"``：字符串自带时区偏移（含 ``Z``），
      ``instant`` 为换算后的 UTC ``datetime``；
    - ``evidence == "missing-offset"``：字符串没有任何偏移信息，
      ``instant`` 为 ``None``，调用方必须按未核查对象处理，禁止猜测本地时区；
    - ``evidence == "unparseable"``：格式无法解析，同样返回 ``None``。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None, "missing-offset"
        return value.astimezone(timezone.utc), "offset"

    text = str(value or "").strip()
    if not text:
        return None, "unparseable"

    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None, "unparseable"

    if parsed.tzinfo is None:
        # 历史快照缺少偏移：不做任何本地时区假设。
        return None, "missing-offset"
    return parsed.astimezone(timezone.utc), "offset"


def to_iso_utc(instant: datetime) -> str:
    """把 UTC 绝对时刻序列化为统一的 ISO-8601 字符串。"""
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------
# 保留边界：保留天数与最低版本数只是初始门槛
# ------------------------------------------------------------------

def build_cutoff(
    retention_days: int,
    *,
    now: datetime | None = None,
) -> datetime:
    """按保留天数计算截止时刻（UTC 绝对时刻）。"""
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return reference - timedelta(days=max(int(retention_days), 0))


def evaluate_snapshots(
    snapshots: list[dict[str, Any]],
    *,
    retention_days: int,
    min_versions: int = 5,
    cutoff: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """对快照集合执行一次完整的保留判定。

    判定流程（保留天数/最低版本数仅作初始门槛）：

    1. 每条快照解析为 UTC 绝对时刻；缺偏移/不可解析的进入 ``unverified``，
       不参与过期判定，也永远不会进入删除队列。
    2. 可解释快照与冻结的截止时刻比较：早于截止时刻为 ``expired``，
       否则为 ``valid``。同一绝对时刻无论原始偏移是 ``+14:00`` 还是
       ``-12:00``，比较结果必然一致。
    3. 最低版本数门槛只在过期集合内生效：为最新的 ``min_versions`` 个
       过期版本打上 ``min_version_protected`` 豁免，其余才允许清理。
    """
    cutoff_utc = (cutoff or build_cutoff(retention_days, now=now)).astimezone(timezone.utc)
    min_versions = max(int(min_versions), 0)

    evaluated: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    valid: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []

    for snap in snapshots:
        instant, evidence = parse_instant(snap.get("created_at"))
        item = {
            "snapshot_id": snap.get("snapshot_id"),
            "table": snap.get("table"),
            "created_at": snap.get("created_at"),
            "evidence": evidence,
            "instant_utc": to_iso_utc(instant) if instant else None,
            "decision": "unverified",
            "reason": "",
        }
        if instant is None:
            item["reason"] = (
                "时间证据缺少UTC偏移，无法解释绝对时刻，不进入删除队列"
                if evidence == "missing-offset"
                else "时间证据格式无法解析，不进入删除队列"
            )
            unverified.append(item)
            evaluated.append(item)
            continue

        if instant < cutoff_utc:
            item["decision"] = "expired"
            item["reason"] = "绝对时刻早于保留截止时刻"
            expired.append(item)
        else:
            item["decision"] = "valid"
            item["reason"] = "绝对时刻不早于保留截止时刻，仍在保留窗口内"
            valid.append(item)
        evaluated.append(item)

    # 最低版本数门槛：只豁免过期集合里最新的 min_versions 个版本。
    expired_sorted = sorted(
        expired, key=lambda item: item["instant_utc"], reverse=True
    )
    protected_ids = {
        item["snapshot_id"] for item in expired_sorted[:min_versions]
    }
    deletable: list[dict[str, Any]] = []
    for item in expired_sorted:
        if item["snapshot_id"] in protected_ids:
            item["decision"] = "min_version_protected"
            item["reason"] = "虽已过期，但受最低版本数门槛保护"
        else:
            item["decision"] = "deletable"
            item["reason"] = "已过期且超出最低版本数门槛，可进入删除队列"
            deletable.append(item)

    return {
        "cutoff_utc": to_iso_utc(cutoff_utc),
        "retention_days": int(retention_days),
        "min_versions_required": min_versions,
        "total_snapshots": len(snapshots),
        "expired_count": len(expired),
        "valid_count": len(valid),
        "unverified_count": len(unverified),
        "protected_count": len(protected_ids),
        "can_clean_count": len(deletable),
        "should_clean": bool(deletable),
        "snapshots": evaluated,
        "deletable": deletable,
        "valid": valid,
        "unverified": unverified,
    }


# ------------------------------------------------------------------
# 计划台账：冻结截止时刻、边界冲突、并发与中断幂等
# ------------------------------------------------------------------

class RetentionRegistry:
    """保留计划与清理执行的内存台账。

    - ``preview`` 首次为规则冻结 cutoff；后续预览沿用同一 cutoff，
      直到计划关闭（执行完成或显式放弃）。
    - 快照时间证据被修正后重新评估，若新旧边界给出不同释放集合，记录
      边界冲突并显式标注最终采用的时间依据。
    - ``execute`` 按作业身份串行化：同一版本并发提交只生效一次；
      执行中断后再次运行沿用首次确定的 cutoff，且已释放对象不重复回收。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plans: dict[int, dict[str, Any]] = {}
        self._executions: dict[int, dict[str, Any]] = {}

    # -- 计划生命周期 -------------------------------------------------

    def preview(
        self,
        rule_entry_id: int,
        snapshots: list[dict[str, Any]],
        *,
        retention_days: int,
        min_versions: int = 5,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            plan = self._plans.get(rule_entry_id)
            if plan is None or plan["status"] == "closed":
                cutoff = build_cutoff(retention_days, now=now)
                plan = {
                    "rule_entry_id": rule_entry_id,
                    "cutoff": cutoff,
                    "status": "open",
                    "revisions": 0,
                    "boundary_conflicts": [],
                    "time_basis": "带偏移UTC绝对时刻",
                    "last_evaluation": None,
                }
                self._plans[rule_entry_id] = plan
            else:
                # 沿用首次冻结的截止时刻，保证中断重跑结果一致。
                cutoff = plan["cutoff"]

            evaluation = evaluate_snapshots(
                snapshots,
                retention_days=retention_days,
                min_versions=min_versions,
                cutoff=cutoff,
            )
            previous = plan["last_evaluation"]
            conflict = None
            if previous is not None:
                old_ids = {
                    item["snapshot_id"] for item in previous.get("deletable", [])
                }
                new_ids = {
                    item["snapshot_id"] for item in evaluation["deletable"]
                }
                if old_ids != new_ids:
                    plan["revisions"] += 1
                    conflict = {
                        "revision": plan["revisions"],
                        "basis": plan["time_basis"],
                        "old_cutoff_utc": previous["cutoff_utc"],
                        "new_cutoff_utc": evaluation["cutoff_utc"],
                        "added_to_release": sorted(new_ids - old_ids),
                        "removed_from_release": sorted(old_ids - new_ids),
                        "resolution": "以可解释的带偏移UTC绝对时刻为准，未核查对象不进入删除队列",
                    }
                    plan["boundary_conflicts"].append(conflict)

            plan["last_evaluation"] = evaluation
            return {
                "plan": self._public_plan(plan),
                "evaluation": evaluation,
                "boundary_conflict": conflict,
            }

    def get_plan(self, rule_entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            plan = self._plans.get(rule_entry_id)
            return self._public_plan(plan) if plan else None

    def close_plan(self, rule_entry_id: int) -> None:
        with self._lock:
            plan = self._plans.get(rule_entry_id)
            if plan is not None:
                plan["status"] = "closed"

    # -- 执行台账：并发只生效一次、中断可续跑 --------------------------

    def execute(
        self,
        job_entry_id: int,
        rule_entry_id: int,
        snapshots: list[dict[str, Any]],
        *,
        retention_days: int,
        min_versions: int = 5,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            execution = self._executions.get(job_entry_id)
            if execution is None:
                preview_result = self.preview(
                    rule_entry_id,
                    snapshots,
                    retention_days=retention_days,
                    min_versions=min_versions,
                    now=now,
                )
                plan = self._plans[rule_entry_id]
                execution = {
                    "job_entry_id": job_entry_id,
                    "rule_entry_id": rule_entry_id,
                    "cutoff": plan["cutoff"],
                    "released": [],
                    "status": "running",
                    "runs": 0,
                }
                self._executions[job_entry_id] = execution
                candidates = [
                    dict(item)
                    for item in preview_result["evaluation"]["deletable"]
                ]
            else:
                # 中断后续跑：沿用首次 cutoff，只处理尚未释放的对象。
                plan = self._plans[rule_entry_id]
                evaluation = evaluate_snapshots(
                    snapshots,
                    retention_days=retention_days,
                    min_versions=min_versions,
                    cutoff=execution["cutoff"],
                )
                released_ids = {item["snapshot_id"] for item in execution["released"]}
                candidates = [
                    dict(item)
                    for item in evaluation["deletable"]
                    if item["snapshot_id"] not in released_ids
                ]

            execution["runs"] += 1
            released_now: list[dict[str, Any]] = []
            for item in candidates:
                # 释放前按对象身份（快照标识）再次核对引用，
                # 无法解释偏移的历史版本根本不会出现在候选集里。
                referenced = self._find_snapshot(snapshots, item["snapshot_id"])
                if referenced is None:
                    item["skip_reason"] = "释放前核对失败：快照引用已不存在"
                    continue
                instant, evidence = parse_instant(referenced.get("created_at"))
                if instant is None or instant >= execution["cutoff"]:
                    item["skip_reason"] = "释放前核对失败：时间证据无法解释或仍在保留窗口内"
                    continue
                released = {
                    "snapshot_id": item["snapshot_id"],
                    "table": referenced.get("table"),
                    "released_at_utc": to_iso_utc(datetime.now(timezone.utc)),
                    "cutoff_utc": to_iso_utc(execution["cutoff"]),
                    "time_basis": "带偏移UTC绝对时刻",
                }
                execution["released"].append(released)
                released_now.append(released)

            execution["status"] = "completed"
            if plan is not None:
                plan["status"] = "closed"
            return {
                "job_entry_id": job_entry_id,
                "rule_entry_id": rule_entry_id,
                "cutoff_utc": to_iso_utc(execution["cutoff"]),
                "runs": execution["runs"],
                "idempotent": execution["runs"] > 1,
                "released_now": released_now,
                "released_total": list(execution["released"]),
                "released_count": len(execution["released"]),
            }

    def get_execution(self, job_entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            return self._executions.get(job_entry_id)

    # -- 内部辅助 -----------------------------------------------------

    @staticmethod
    def _find_snapshot(
        snapshots: list[dict[str, Any]], snapshot_id: Any
    ) -> dict[str, Any] | None:
        for snap in snapshots:
            if snap.get("snapshot_id") == snapshot_id:
                return snap
        return None

    def _public_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "rule_entry_id": plan["rule_entry_id"],
            "cutoff_utc": to_iso_utc(plan["cutoff"]),
            "status": plan["status"],
            "revisions": plan["revisions"],
            "time_basis": plan["time_basis"],
            "boundary_conflicts": list(plan["boundary_conflicts"]),
        }


registry = RetentionRegistry()
