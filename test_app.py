import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app import BusinessError, RandomizationStore


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = [
            self.store.enroll("site1", self.trial["id"], f"S001-{i:03d}", {"risk": "low"})
            for i in range(1, 5)
        ]
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 2, "B": 2}))
        request = self.store.request_unblinding("site1", participants[0]["id"], "受试者发生严重不良事件需要紧急处理")
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})

    def test_idempotent_enrollment_site_isolation_and_protocol_lock(self):
        first = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        again = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_participant("site2", first["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_protocol("coord", self.trial["id"], "v2")
        self.assertEqual(ctx.exception.code, "protocol_locked")


class ProtocolAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降糖研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-002"
        )
        self.store.start_trial("coord", self.trial["id"])
        # 旧规则(risk)下先入组两例，触发为旧分层预留区组随机号。
        self.old1 = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        self.old2 = self.store.enroll("site1", self.trial["id"], "S001-002", {"risk": "high"})

    def tearDown(self):
        self.tmp.cleanup()

    def _old_unused_allocations(self):
        with self.store.connect() as conn:
            return {r["id"] for r in conn.execute(
                "SELECT id FROM allocations WHERE trial_id=? AND revision_no=0 AND used_by IS NULL",
                (self.trial["id"],),
            ).fetchall()}

    def test_pending_amendment_pauses_enrollment(self):
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v2.0", ["A", "B"], ["risk", "ageband"]
        )
        self.assertEqual(amendment["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low", "ageband": "lt65"})
        self.assertEqual(ctx.exception.code, "enrollment_paused_amendment_pending")
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertTrue(summary["enrollment_paused"])
        self.assertEqual(summary["pending_amendment_id"], amendment["id"])
        # 不能重复提交；站点和非监查员无权确认。
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_amendment("coord", self.trial["id"], "v2.1", ["A", "B"], ["risk", "ageband"])
        self.assertEqual(ctx.exception.code, "amendment_pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_amendment("coord", amendment["id"])
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_two_monitor_confirmation_then_new_rules_apply(self):
        old_unused = self._old_unused_allocations()
        self.assertGreater(len(old_unused), 0)
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v2.0", ["A", "B"], ["risk", "ageband"]
        )
        first = self.store.confirm_amendment("monitor1", amendment["id"])
        self.assertEqual(first["status"], "pending")
        # 第一人确认期间仍然暂停。
        with self.assertRaises(BusinessError):
            self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low", "ageband": "lt65"})
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_amendment("monitor1", amendment["id"])
        self.assertEqual(ctx.exception.code, "distinct_confirmer_required")
        second = self.store.confirm_amendment("monitor2", amendment["id"])
        self.assertEqual(second["status"], "effective")
        self.assertIsNotNone(second["effective_at"])

        with self.store.connect() as conn:
            trial = conn.execute("SELECT * FROM trials WHERE id=?", (self.trial["id"],)).fetchone()
            self.assertEqual(trial["revision_no"], 1)
            self.assertEqual(json.loads(trial["strata_factors_json"]), ["risk", "ageband"])

        # 生效后下一例按新规则随机；旧分层因素不再被接受。
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "invalid_factors")
        new1 = self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low", "ageband": "lt65"})
        new2 = self.store.enroll("site1", self.trial["id"], "S001-004", {"risk": "low", "ageband": "ge65"})

        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT p.id,p.stratum_id,s.revision_no AS stratum_rev,a.id AS alloc_id,a.revision_no AS alloc_rev
                   FROM participants p JOIN strata s ON s.id=p.stratum_id JOIN allocations a ON a.id=p.allocation_id
                   WHERE p.trial_id=? ORDER BY p.id""", (self.trial["id"],)
            ).fetchall()
            by_id = {r["id"]: dict(r) for r in rows}
            # 早先入组的受试者保留在修订0（旧分组规则）。
            self.assertEqual(by_id[self.old1["id"]]["stratum_rev"], 0)
            self.assertEqual(by_id[self.old1["id"]]["alloc_rev"], 0)
            self.assertEqual(by_id[self.old2["id"]]["stratum_rev"], 0)
            # 新例走修订1，且使用的随机号绝不来自旧规则预留池。
            for pid in (new1["id"], new2["id"]):
                self.assertEqual(by_id[pid]["stratum_rev"], 1)
                self.assertEqual(by_id[pid]["alloc_rev"], 1)
                self.assertNotIn(by_id[pid]["alloc_id"], old_unused)
            still_unused = {r["id"] for r in conn.execute(
                "SELECT id FROM allocations WHERE revision_no=0 AND used_by IS NULL")}
            self.assertEqual(still_unused, old_unused)  # 旧预留号原封不动
            # 修订0与修订1即使分层键相同也分别建档。
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM strata WHERE trial_id=? AND revision_no=1", (self.trial["id"],)
            ).fetchone()[0], 2)

        # 修订生效后不能再改。
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_amendment("coord", self.trial["id"], "v3.0", ["A", "B", "C"], ["risk"])
        self.assertEqual(ctx.exception.code, "amendment_locked")
        # 已生效的修订不能重复确认。
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_amendment("monitor1", amendment["id"])
        self.assertEqual(ctx.exception.code, "amendment_closed")
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertFalse(summary["enrollment_paused"])
        self.assertTrue(summary["amendments_locked"])
        self.assertEqual(summary["amendments"][0]["status"], "effective")
        revs = {r["revision_no"]: r["reserved_unused"] for r in summary["reserved_unused_by_revision"]}
        self.assertIn(0, revs)

    def test_rejection_resumes_enrollment_under_old_rules(self):
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v2.0", ["A", "B"], ["risk", "ageband"]
        )
        rejected = self.store.reject_amendment("monitor1", amendment["id"], "分层定义与统计计划不符")
        self.assertEqual(rejected["status"], "rejected")
        # 驳回后按旧规则继续入组，且可以重新提交新的修订。
        before = self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low"})
        again = self.store.submit_amendment("coord", self.trial["id"], "v2.0b", ["A", "B"], ["site_tier"])
        self.assertEqual(again["revision_no"], 2)
        items = self.store.list_amendments("coord", self.trial["id"])
        self.assertEqual([a["revision_no"] for a in items], [1, 2])
        self.assertEqual([a["status"] for a in items], ["rejected", "pending"])
        with self.assertRaises(BusinessError):
            self.store.enroll("site1", self.trial["id"], "S001-004", {"site_tier": "t1"})
        self.assertGreater(before["id"], self.old2["id"])

    def test_site_cannot_see_amended_arms_but_sees_status(self):
        self.store.submit_amendment("coord", self.trial["id"], "v2.0", ["A", "B"], ["risk", "ageband"])
        items = self.store.list_amendments("site1", self.trial["id"])
        self.assertEqual(len(items), 1)
        self.assertNotIn("arms", items[0])
        self.assertEqual(items[0]["strata_factors"], ["risk", "ageband"])
        full = self.store.list_amendments("monitor2", self.trial["id"])
        self.assertEqual(full[0]["arms"], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
