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

    def test_amendment_pause_dual_confirm_and_revision_switch(self):
        # 修订前先入组两例：区组预留 4 个随机号，后两个未使用
        p1 = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        self.store.enroll("site1", self.trial["id"], "S001-002", {"risk": "low"})
        self.assertEqual(p1["revision_no"], 0)
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v2.0", ["A", "B"], ["risk"], 4, "seed-2026-amend"
        )
        self.assertEqual(amendment["revision_no"], 1)
        self.assertEqual(amendment["status"], "pending")
        self.assertIsNone(amendment["effective_at"])
        # 待确认期间入组暂停，但已入组者的幂等重放不受影响
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "enrollment_paused")
        replay = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        self.assertTrue(replay["idempotent"])
        # 角色限制：中心不能提交，协调员不能确认
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_amendment("site1", self.trial["id"], "v2.0", ["A", "B"], ["risk"], 4, "seed-2026-amend")
        self.assertEqual(ctx.exception.code, "forbidden")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_amendment("coord", amendment["id"])
        self.assertEqual(ctx.exception.code, "forbidden")
        # 两位监查员分别确认，同一人不能确认两次
        first = self.store.confirm_amendment("monitor1", amendment["id"])
        self.assertEqual(first["status"], "pending")
        self.assertTrue(first["second_confirmation_required"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_amendment("monitor1", amendment["id"])
        self.assertEqual(ctx.exception.code, "distinct_confirmer_required")
        second = self.store.confirm_amendment("monitor2", amendment["id"])
        self.assertEqual(second["status"], "effective")
        self.assertIsNotNone(second["effective_at"])
        # 第二人同意后下一例按新规则随机
        p3 = self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low"})
        self.assertEqual(p3["revision_no"], 1)
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT revision_no, sequence, used_by FROM allocations WHERE stratum_id=? ORDER BY sequence",
                (conn.execute("SELECT stratum_id FROM participants WHERE id=?", (p1["id"],)).fetchone()[0],),
            ).fetchall()
        old = [r for r in rows if r["revision_no"] == 0]
        new = [r for r in rows if r["revision_no"] == 1]
        # 旧规则预留但未使用的随机号（第 3、4 个）不能发给新规则下的受试者
        self.assertEqual([r["used_by"] for r in old], [p1["id"], self._participant_id("S001-002"), None, None])
        self.assertEqual(len(new), 4)
        self.assertEqual([r["used_by"] is not None for r in new], [True, False, False, False])
        self.assertEqual([r for r in new if r["used_by"]][0]["used_by"], p3["id"])
        # 早先入组的受试者保留原分组（revision_no=0 不动）
        # 修订生效后不能再改
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_amendment("coord", self.trial["id"], "v3.0", ["A", "B"], ["risk"], 4, "seed-2026-third")
        self.assertEqual(ctx.exception.code, "amendment_locked")
        # 已结束的修订不能重复确认
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_amendment("monitor1", amendment["id"])
        self.assertEqual(ctx.exception.code, "amendment_decided")

    def _participant_id(self, external_id):
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT id FROM participants WHERE trial_id=? AND external_id=?", (self.trial["id"], external_id)
            ).fetchone()[0]

    def test_amendment_rejection_resumes_and_summary_shows_states(self):
        self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v2.0", ["A", "B"], ["risk"], 4, "seed-2026-amend"
        )
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertTrue(summary["enrollment_paused"])
        self.assertEqual(len(summary["amendments"]), 1)
        # 驳回后入组恢复，并可重新提交修订
        self.store.reject_amendment("monitor1", amendment["id"], "分层因素描述需补充")
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertFalse(summary["enrollment_paused"])
        self.assertEqual(summary["amendments"][0]["status"], "rejected")
        p = self.store.enroll("site1", self.trial["id"], "S001-002", {"risk": "low"})
        self.assertEqual(p["revision_no"], 0)
        second = self.store.submit_amendment(
            "coord", self.trial["id"], "v2.1", ["A", "B"], ["risk"], 4, "seed-2026-amend2"
        )
        self.assertEqual(second["revision_no"], 2)
        self.store.confirm_amendment("monitor1", second["id"])
        self.store.confirm_amendment("monitor2", second["id"])
        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertEqual(summary["current_revision"]["revision_no"], 2)
        self.assertEqual(summary["current_revision"]["protocol_version"], "v2.1")
        statuses = [(a["revision_no"], a["status"]) for a in summary["amendments"]]
        self.assertEqual(statuses, [(1, "rejected"), (2, "effective")])
        self.assertIsNotNone(summary["amendments"][1]["effective_at"])

    def test_amendment_new_strata_factors_apply_to_next_enrollment(self):
        self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v2.0", ["A", "B"], ["stage"], 4, "seed-2026-stage"
        )
        self.store.confirm_amendment("monitor1", amendment["id"])
        self.store.confirm_amendment("monitor2", amendment["id"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial["id"], "S001-002", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "invalid_factors")
        p = self.store.enroll("site1", self.trial["id"], "S001-002", {"stage": "III"})
        self.assertEqual(p["revision_no"], 1)

    def test_amendment_requires_running_trial_and_no_concurrent_pending(self):
        draft = self.store.create_trial(
            "coord", "草稿研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-draft"
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_amendment("coord", draft["id"], "v2.0", ["A", "B"], ["risk"], 4, "seed-2026-x")
        self.assertEqual(ctx.exception.code, "trial_not_running")
        self.store.submit_amendment("coord", self.trial["id"], "v2.0", ["A", "B"], ["risk"], 4, "seed-2026-amend")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_amendment("coord", self.trial["id"], "v2.1", ["A", "B"], ["risk"], 4, "seed-2026-amend2")
        self.assertEqual(ctx.exception.code, "amendment_pending")


if __name__ == "__main__":
    unittest.main()
