"""快照保留场景的共享数据装配。

规则详情（retention_policy）与清理列表（vacuum_job）两个入口必须读取
同一份快照绝对时刻，因此把"按湖表从快照仓库装配快照集合"的逻辑收敛到
这里，避免两个服务各自解释时间字符串导致判定不一致。
"""
from __future__ import annotations

from typing import Any

from app.store import store

# 快照时间字段与保留引擎约定字段的映射。
TIME_FIELD = "快照时间"
ID_FIELD = "快照标识"
TABLE_FIELD = "目标湖表"


def load_table_snapshots(table: str | None = None) -> list[dict[str, Any]]:
    """从快照管理模块装配快照集合。

    只做字段搬运，不做任何时区换算；时区换算统一由
    ``app.retention_engine.parse_instant`` 完成，保证两个入口拿到的
    原始时间证据和绝对时刻完全一致。
    """
    rows = store.rows("snapshot_mgr")
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        if table and str(row.get(TABLE_FIELD, "")) != str(table):
            continue
        snapshots.append(
            {
                "snapshot_id": row.get(ID_FIELD),
                "table": row.get(TABLE_FIELD),
                "created_at": row.get(TIME_FIELD),
                "status": row.get("status"),
                "ref_id": row.get("id"),
            }
        )
    return snapshots


def find_snapshot_identity(snapshot_id: Any) -> dict[str, Any] | None:
    """按对象身份（快照标识）定位仓库中的快照行，供释放前核对引用。"""
    for row in store.rows("snapshot_mgr"):
        if row.get(ID_FIELD) == snapshot_id:
            return row
    return None


def mark_snapshot_cleaned(snapshot_id: Any) -> bool:
    """物理回收后回写快照保留状态，返回是否命中并更新。"""
    row = find_snapshot_identity(snapshot_id)
    if row is None:
        return False
    row["status"] = "已清理"
    row["pending"] = False
    row["abnormal"] = False
    row["保留状态"] = "已物理回收"
    return True


def repair_snapshot_offset(snapshot_id: Any, offset_value: str) -> dict[str, Any]:
    """补正一条缺少偏移的历史快照的时间证据。

    ``offset_value`` 形如 ``2024-10-10T00:00:00+14:00``；补正后保留原始
    证据痕迹，便于在边界冲突清单里追溯采用的时间依据。
    """
    row = find_snapshot_identity(snapshot_id)
    if row is None:
        raise KeyError(f"快照 {snapshot_id} 不存在")
    row.setdefault("时间证据历史", []).append(row.get(TIME_FIELD))
    row[TIME_FIELD] = offset_value
    return row
