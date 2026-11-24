"""快照保留治理端到端测试：规则详情与清理列表同源、证据补正、边界冲突、
清理执行幂等，以及 +14:00 / -12:00 等价时刻判定一致。"""
from __future__ import annotations

import sys

sys.path.insert(0, '.')

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)

RULE_ID = 4        # rule-retention-tz-demo
JOB_ID = 4         # vacuum-retention-tz-demo
TABLE = "retention_tz_demo"


def _snapshot_rows():
    return [row for row in store.rows("snapshot_mgr") if row.get("目标湖表") == TABLE]


def _by_id(payload: dict, snapshot_id: str) -> dict:
    return next(item for item in payload["snapshots"] if item["snapshot_id"] == snapshot_id)


class TestEquivalentOffsets:
    def test_plus14_minus12_same_verdict(self):
        resp = client.get(f"/api/retention/rules/{RULE_ID}/preview")
        assert resp.status_code == 200
        data = resp.json()
        plus14 = _by_id(data, "snap-tz-plus14")
        minus12 = _by_id(data, "snap-tz-minus12")
        assert plus14["instant_utc"] == minus12["instant_utc"]
        assert plus14["decision"] == minus12["decision"]
        assert plus14["decision"] in {"deletable", "min_version_protected"}

    def test_rule_detail_and_clean_list_share_cutoff(self):
        rule_view = client.get(f"/api/retention/rules/{RULE_ID}/preview").json()
        list_view = client.get(f"/api/retention/jobs/{JOB_ID}/clean-list").json()
        assert rule_view["cutoff_utc"] == list_view["cutoff_utc"]
        assert rule_view["time_basis"] == list_view["time_basis"] == "带偏移UTC绝对时刻"
        rule_release = {item["snapshot_id"] for item in rule_view["deletable"]}
        list_release = {item["snapshot_id"] for item in list_view["clean_list"]}
        assert rule_release == list_release


class TestMissingOffsetProtection:
    def test_naive_snapshot_listed_as_unverified_not_deletable(self):
        data = client.get(f"/api/retention/rules/{RULE_ID}/preview").json()
        unverified_ids = {item["snapshot_id"] for item in data["unverified"]}
        assert "snap-naive-legacy" in unverified_ids
        deletable_ids = {item["snapshot_id"] for item in data["deletable"]}
        assert "snap-naive-legacy" not in deletable_ids
        assert data["unverified_count"] >= 1


class TestEvidenceRepairAndBoundaryConflict:
    def test_repair_then_conflict_marked(self):
        before = client.get(f"/api/retention/rules/{RULE_ID}/preview").json()
        before_release = {item["snapshot_id"] for item in before["deletable"]}

        resp = client.post(
            "/api/retention/evidence/repair",
            json={
                "snapshot_id": "snap-naive-legacy",
                "created_at": "2024-07-20T10:00:00+14:00",
            },
        )
        assert resp.status_code == 200
        repair = resp.json()
        assert repair["ok"] is True
        assert repair["instant_utc"] == "2024-07-19T20:00:00Z"

        after = client.get(f"/api/retention/rules/{RULE_ID}/preview").json()
        after_release = {item["snapshot_id"] for item in after["deletable"]}
        if before_release != after_release:
            conflict = after["boundary_conflict"] or repair["boundary_conflict"]
            assert conflict is not None
            assert conflict["basis"] == "带偏移UTC绝对时刻"
        # 补正后该快照不再是未核查对象。
        unverified_ids = {item["snapshot_id"] for item in after["unverified"]}
        assert "snap-naive-legacy" not in unverified_ids
        # 截止时刻沿用首次预览，没有因重算漂移。
        assert before["cutoff_utc"] == after["cutoff_utc"]


class TestExecuteCleanup:
    def test_execute_idempotent_and_write_back(self):
        first = client.post(f"/api/retention/jobs/{JOB_ID}/execute")
        assert first.status_code == 200
        first_data = first.json()
        released_count_first = first_data["released_count"]
        cutoff = first_data["cutoff_utc"]

        second = client.post(f"/api/retention/jobs/{JOB_ID}/execute").json()
        assert second["idempotent"] is True
        assert second["cutoff_utc"] == cutoff
        assert second["runs"] == 2
        assert second["files_cleaned"] == 0  # 第二次没有新对象可回收
        assert second["released_count"] == released_count_first

        # 释放结果回写保留策略待办。
        rule = store.find("retention_policy", RULE_ID)
        assert cutoff in str(rule.get("执行结果"))

        # 被回收的快照保留状态已回写为已清理。
        cleaned = [row for row in _snapshot_rows() if row["status"] == "已清理"]
        assert len(cleaned) == released_count_first
        # 仍是缺偏移、未核查的对象绝不允许被回收。
        from app.retention_engine import parse_instant

        for row in cleaned:
            _, evidence = parse_instant(row.get("快照时间"))
            assert evidence == "offset"

    def test_clean_list_after_execute_and_remaining_unverified(self):
        refreshed = client.get(f"/api/retention/jobs/{JOB_ID}/clean-list").json()
        # 清理列表与规则详情仍返回同一截止时刻。
        rule_view = client.get(f"/api/retention/rules/{RULE_ID}/preview").json()
        assert refreshed["cutoff_utc"] == rule_view["cutoff_utc"]
        # 计划执行后关闭，剩余待核查项仍被明确列出（补正过的表若无缺偏移则为 0）。
        assert "unverified" in refreshed
        assert isinstance(refreshed["unverified_count"], int)
