import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import revision_rules
from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
        self.assertEqual(ctx.exception.status, 403)


class RevisionFlowTests(unittest.TestCase):
    """大修返修交付：修订稿、版本存档、原评审人新版意见与改判。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _decided_paper(self, decision="major_revision"):
        paper_id = self.store.submit_paper(
            "alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。"
        )["id"]
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 3, "方法可行，但需要大幅补充对比实验。")
        self.store.submit_review("r2", a2, 2, "写作混乱，实验设计存在明显缺陷。")
        self.store.decide("chair", paper_id, decision, "请按评审意见修改。")
        return paper_id

    def _revision_args(self):
        return {
            "title": "可靠分布式提交协议（修订版）",
            "abstract": "本文提出一种用于弱网环境的可靠提交协议，修订版补充了对比实验并重写了实验设计章节。",
            "response": "意见一：已补充与最近工作的对比实验；意见二：已重写实验设计章节。",
        }

    def test_revision_flow_returns_to_review_and_allows_redecision(self):
        paper_id = self._decided_paper()
        result = self.store.submit_revision("alice", paper_id, **self._revision_args())
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["status"], "under_review")
        self.assertIn("deadline", result)
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "under_review")

        # 旧版继续可查，新版为第 2 版，内容快照均保留。
        versions = self.store.list_versions("chair", paper_id)
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["title"], "可靠分布式提交协议")
        self.assertEqual(versions[1]["title"], "可靠分布式提交协议（修订版）")
        self.assertNotEqual(versions[0]["content_hash"], versions[1]["content_hash"])

        # 只有一位原评审人提交新版意见时，主席不能改判。
        progress = self.store.submit_revision_review("r1", paper_id, 4, "新版补充了对比实验，质量明显提升。")
        self.assertEqual((progress["completed"], progress["required"]), (1, 2))
        status = self.store.get_revision("chair", paper_id)
        self.assertEqual((status["completed"], status["required"]), (1, 2))
        self.assertEqual(status["reviews"][0]["reviewer_id"], "r1")
        # 作者视角下评审人匿名。
        self.assertEqual(self.store.get_revision("alice", paper_id)["reviews"][0]["reviewer_id"], "reviewer#1")
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept", "提前改判。")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "revision_reviews_incomplete"))

        # 两位原评审人都完成后才能改判。
        self.store.submit_revision_review("r2", paper_id, 4, "实验设计章节重写后已可接收。")
        final = self.store.decide("chair", paper_id, "accept", "修改到位，接收。")
        self.assertEqual(final["decision"], "accept")
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "decided")

        # 原有意见仅留档：原评审分数与状态保持不动，审计历史包含两轮决定。
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT reviewer_id,score,status FROM assignments WHERE paper_id=? ORDER BY reviewer_id", (paper_id,)
            ).fetchall()
        self.assertEqual([(r["reviewer_id"], r["score"], r["status"]) for r in rows],
                         [("r1", 3, "completed"), ("r2", 2, "completed")])
        actions = [h["action"] for h in self.store.history("chair", paper_id)]
        self.assertIn("revision.submit", actions)
        self.assertEqual(actions.count("decision.record"), 2)

    def test_revision_overdue_and_duplicate_return_409(self):
        paper_id = self._decided_paper()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE decisions SET created_at=? WHERE paper_id=?",
                ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds"), paper_id),
            )
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision("alice", paper_id, **self._revision_args())
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "revision_overdue"))

        fresh = self._decided_paper()
        self.store.submit_revision("alice", fresh, **self._revision_args())
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision("alice", fresh, **self._revision_args())
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "revision_exists"))

    def test_revision_requires_revisable_decision(self):
        paper_id = self._decided_paper("accept")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision("alice", paper_id, **self._revision_args())
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "revision_not_allowed"))
        undecided = self.store.submit_paper(
            "alice", "另一篇分布式协议论文", "这篇论文尚未进入决定阶段，用于验证未决定时不能返修。"
        )["id"]
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision("alice", undecided, **self._revision_args())
        self.assertEqual(ctx.exception.code, "revision_not_allowed")

    def test_revision_review_only_from_original_reviewers_once(self):
        paper_id = self._decided_paper()
        self.store.submit_revision("alice", paper_id, **self._revision_args())
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision_review("r3", paper_id, 4, "我不是原评审人，不能提交新版意见。")
        self.assertEqual(ctx.exception.status, 403)
        self.store.submit_revision_review("r1", paper_id, 4, "新版补充了对比实验，质量明显提升。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision_review("r1", paper_id, 5, "重复提交新版意见应被拒绝。")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "revision_review_exists"))

    def test_revision_review_requires_open_revision(self):
        paper_id = self._decided_paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision_review("r1", paper_id, 4, "论文尚未进入返修评审阶段，不能提交。")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "revision_not_open"))


class RevisionRulesTests(unittest.TestCase):
    def test_deadline_and_overdue(self):
        decided = "2026-09-20T08:00:00+00:00"
        self.assertEqual(revision_rules.REVISION_WINDOW_DAYS, 7)
        self.assertEqual(
            revision_rules.deadline_for(decided),
            datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(revision_rules.is_overdue(decided, datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)))
        self.assertTrue(revision_rules.is_overdue(decided, datetime(2026, 9, 27, 8, 1, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
