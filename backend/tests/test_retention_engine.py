"""保留判定引擎测试：时区换算、初始门槛、边界冲突、并发与中断幂等。"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '.')

from app.retention_engine import (
    RetentionRegistry,
    build_cutoff,
    evaluate_snapshots,
    parse_instant,
    to_iso_utc,
)

# 固定“当前时刻”，保证测试可重复。
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)

# 同一绝对时刻的两种偏移表达：
# 2024-10-09T10:00:00Z == 2024-10-10T00:00:00+14:00 == 2024-10-08T22:00:00-12:00
EQUIVALENT_PLUS14 = "2024-10-10T00:00:00+14:00"
EQUIVALENT_MINUS12 = "2024-10-08T22:00:00-12:00"
EQUIVALENT_UTC = "2024-10-09T10:00:00Z"


class TestParseInstant:
    def test_equivalent_offsets_same_instant(self):
        instant_plus14, evidence_a = parse_instant(EQUIVALENT_PLUS14)
        instant_minus12, evidence_b = parse_instant(EQUIVALENT_MINUS12)
        instant_utc, evidence_c = parse_instant(EQUIVALENT_UTC)
        assert evidence_a == evidence_b == evidence_c == "offset"
        assert instant_plus14 == instant_minus12 == instant_utc
        assert to_iso_utc(instant_plus14) == "2024-10-09T10:00:00Z"

    def test_missing_offset_is_not_guessed(self):
        instant, evidence = parse_instant("2024-07-01T10:00:00")
        assert instant is None
        assert evidence == "missing-offset"

    def test_unparseable(self):
        instant, evidence = parse_instant("not-a-date")
        assert instant is None
        assert evidence == "unparseable"

    def test_z_suffix(self):
        instant, evidence = parse_instant("2024-09-01T08:30:00Z")
        assert evidence == "offset"
        assert instant.utcoffset() == timedelta(0)


class TestTimezoneEquivalentDecision:
    def _snapshots(self) -> list[dict]:
        return [
            {"snapshot_id": "plus14", "created_at": EQUIVALENT_PLUS14},
            {"snapshot_id": "minus12", "created_at": EQUIVALENT_MINUS12},
            {"snapshot_id": "utc", "created_at": EQUIVALENT_UTC},
        ]

    def test_same_decision_across_offsets(self):
        cutoff = build_cutoff(30, now=NOW)  # 2026-10-25，三条都过期
        result = evaluate_snapshots(
            self._snapshots(), retention_days=30, min_versions=0, cutoff=cutoff
        )
        decisions = {item["snapshot_id"]: item["decision"] for item in result["snapshots"]}
        assert len(set(decisions.values())) == 1
        assert decisions["plus14"] == decisions["minus12"] == decisions["utc"]

    def test_equivalent_instants_near_boundary(self):
        # 截止时刻恰好等于该绝对时刻：不早于 cutoff => 有效。
        cutoff = datetime(2024, 10, 9, 10, 0, 0, tzinfo=timezone.utc)
        result = evaluate_snapshots(
            self._snapshots(), retention_days=999, min_versions=0, cutoff=cutoff
        )
        assert all(item["decision"] == "valid" for item in result["snapshots"])

    def test_plus14_and_minus12_full_report_identical(self):
        cutoff = build_cutoff(700, now=NOW)
        a = evaluate_snapshots(
            [{"snapshot_id": "x", "created_at": EQUIVALENT_PLUS14}],
            retention_days=700, min_versions=0, cutoff=cutoff,
        )
        b = evaluate_snapshots(
            [{"snapshot_id": "x", "created_at": EQUIVALENT_MINUS12}],
            retention_days=700, min_versions=0, cutoff=cutoff,
        )
        assert a["expired_count"] == b["expired_count"]
        assert a["valid_count"] == b["valid_count"]
        assert a["can_clean_count"] == b["can_clean_count"]
        assert a["snapshots"][0]["instant_utc"] == b["snapshots"][0]["instant_utc"]


class TestInitialThresholds:
    def test_retention_days_gate(self):
        snapshots = [
            {"snapshot_id": "old", "created_at": "2024-01-01T00:00:00Z"},
            {"snapshot_id": "new", "created_at": "2026-11-01T00:00:00Z"},
        ]
        result = evaluate_snapshots(
            snapshots, retention_days=30, min_versions=0,
            cutoff=build_cutoff(30, now=NOW),
        )
        ids = {item["snapshot_id"]: item["decision"] for item in result["snapshots"]}
        assert ids["old"] == "deletable"
        assert ids["new"] == "valid"

    def test_min_versions_protects_newest_expired(self):
        snapshots = [
            {"snapshot_id": f"old-{i}", "created_at": f"2024-01-0{i+1}T00:00:00Z"}
            for i in range(4)
        ]
        result = evaluate_snapshots(
            snapshots, retention_days=30, min_versions=2,
            cutoff=build_cutoff(30, now=NOW),
        )
        # 4 条全部过期，最新 2 条受最低版本数保护，最旧 2 条可清理。
        decisions = {item["snapshot_id"]: item["decision"] for item in result["snapshots"]}
        assert decisions["old-3"] == "min_version_protected"
        assert decisions["old-2"] == "min_version_protected"
        assert decisions["old-0"] == "deletable"
        assert decisions["old-1"] == "deletable"
        assert result["can_clean_count"] == 2

    def test_missing_offset_never_deletable(self):
        snapshots = [
            {"snapshot_id": "naive", "created_at": "2024-01-01T10:00:00"},
            {"snapshot_id": "aware", "created_at": "2024-01-01T10:00:00Z"},
        ]
        result = evaluate_snapshots(
            snapshots, retention_days=30, min_versions=0,
            cutoff=build_cutoff(30, now=NOW),
        )
        decisions = {item["snapshot_id"]: item["decision"] for item in result["snapshots"]}
        assert decisions["naive"] == "unverified"
        assert decisions["aware"] == "deletable"
        assert result["unverified_count"] == 1
        assert all(item["snapshot_id"] != "naive" for item in result["deletable"])


class TestRegistryPlan:
    def test_cutoff_frozen_across_previews(self):
        registry = RetentionRegistry()
        snapshots = [{"snapshot_id": "s1", "created_at": "2024-01-01T00:00:00Z"}]
        first = registry.preview(1, snapshots, retention_days=30, now=NOW)
        later_now = NOW + timedelta(days=40)
        second = registry.preview(1, snapshots, retention_days=30, now=later_now)
        assert first["plan"]["cutoff_utc"] == second["plan"]["cutoff_utc"]

    def test_boundary_conflict_recorded_after_evidence_repair(self):
        registry = RetentionRegistry()
        snapshots_v1 = [
            {"snapshot_id": "a", "created_at": "2024-01-01T00:00:00Z"},
            {"snapshot_id": "naive", "created_at": "2024-07-01T10:00:00"},
        ]
        first = registry.preview(
            2, snapshots_v1, retention_days=30, min_versions=0, now=NOW
        )
        assert first["boundary_conflict"] is None
        # 补正缺偏移证据：该快照实际也已过期，释放集合发生变化。
        snapshots_v2 = [
            {"snapshot_id": "a", "created_at": "2024-01-01T00:00:00Z"},
            {"snapshot_id": "naive", "created_at": "2024-07-01T10:00:00Z"},
        ]
        second = registry.preview(
            2, snapshots_v2, retention_days=30, min_versions=0, now=NOW
        )
        conflict = second["boundary_conflict"]
        assert conflict is not None
        assert "naive" in conflict["added_to_release"]
        assert conflict["basis"] == "带偏移UTC绝对时刻"
        assert second["plan"]["revisions"] == 1

    def test_conflict_marks_basis_and_unverified(self):
        registry = RetentionRegistry()
        snapshots = [
            {"snapshot_id": "a", "created_at": "2024-01-01T00:00:00Z"},
            {"snapshot_id": "naive", "created_at": "2024-07-01T10:00:00"},
        ]
        result = registry.preview(
            3, snapshots, retention_days=30, min_versions=0, now=NOW
        )
        assert result["plan"]["time_basis"] == "带偏移UTC绝对时刻"
        assert [item["snapshot_id"] for item in result["evaluation"]["unverified"]] == ["naive"]


class TestExecutionIdempotency:
    def _snapshots(self) -> list[dict]:
        return [
            {"snapshot_id": f"old-{i}", "created_at": f"2024-01-0{i+1}T00:00:00Z"}
            for i in range(3)
        ] + [{"snapshot_id": "new", "created_at": "2026-11-01T00:00:00Z"}]

    def test_concurrent_submit_takes_effect_once(self):
        registry = RetentionRegistry()
        outcomes: list[dict] = []
        barrier = threading.Barrier(4)

        def worker() -> None:
            barrier.wait()
            outcomes.append(
                registry.execute(
                    10, 1, self._snapshots(),
                    retention_days=30, min_versions=0, now=NOW,
                )
            )

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        cutoffs = {item["cutoff_utc"] for item in outcomes}
        assert len(cutoffs) == 1
        # 只有首次真正释放，后续提交都是幂等空跑。
        total_released = [item["released_count"] for item in outcomes]
        assert total_released[0] == 3
        assert all(count == 3 for count in total_released)
        assert sum(len(item["released_now"]) for item in outcomes) == 3

    def test_interrupted_rerun_keeps_first_cutoff(self):
        registry = RetentionRegistry()
        first = registry.execute(
            11, 1, self._snapshots(),
            retention_days=30, min_versions=0, now=NOW,
        )
        second_now = NOW + timedelta(days=60)
        second = registry.execute(
            11, 1, self._snapshots(),
            retention_days=30, min_versions=0, now=second_now,
        )
        assert first["cutoff_utc"] == second["cutoff_utc"]
        assert second["idempotent"] is True
        assert second["runs"] == 2
        assert second["released_now"] == []
        assert second["released_count"] == first["released_count"]

    def test_release_skips_unreferenced_and_unverifiable(self):
        registry = RetentionRegistry()
        # 计划基于含 3 条可解释过期快照的集合生成。
        registry.preview(1, self._snapshots(), retention_days=30,
                         min_versions=0, now=NOW)
        # 执行时集合被篡改：一条引用消失、一条丢失偏移。
        tampered = [
            {"snapshot_id": "old-1", "created_at": "2024-01-02T00:00:00Z"},
            {"snapshot_id": "old-2", "created_at": "2024-01-03T00:00:00"},
            {"snapshot_id": "new", "created_at": "2026-11-01T00:00:00Z"},
        ]
        result = registry.execute(
            12, 1, tampered, retention_days=30, min_versions=0, now=NOW,
        )
        released_ids = {item["snapshot_id"] for item in result["released_now"]}
        assert "old-0" not in released_ids  # 引用不存在，跳过
        assert "old-2" not in released_ids  # 偏移无法解释，跳过
        assert released_ids == {"old-1"}
