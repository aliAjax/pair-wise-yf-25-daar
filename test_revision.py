import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import BusinessError, ReviewStore, utcnow


class FakeClock:
    """可拨动的时钟，驱动返修七天窗口测试。"""

    def __init__(self, moment: datetime):
        self.moment = moment

    def __call__(self) -> str:
        return self.moment.isoformat(timespec="seconds")

    def advance(self, **delta) -> None:
        self.moment += timedelta(**delta)


class RevisionFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.clock = FakeClock(base)
        self.store = ReviewStore(Path(self.tmp.name) / "test.db", clock=self.clock)
        self.store.seed()
        self.paper_id = self._decided_major_revision()

    def tearDown(self):
        self.tmp.cleanup()

    def _decided_major_revision(self) -> int:
        paper_id = self.store.submit_paper(
            "alice", "旧版标题：一致性协议", "这是旧版摘要，描述一个一致性协议的初始设计，至少二十个字。"
        )["id"]
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 2, "方法存在明显漏洞，需要大修并补充证明。")
        self.store.submit_review("r2", a2, 2, "实验设计不充分，结论站不住脚，大修。")
        self.store.decide("chair", paper_id, "major_revision", "请大修后重新提交。")
        return paper_id

    def _revision_payload(self, **overrides):
        payload = {
            "title": "新版标题：一致性协议修订版",
            "abstract": "新版摘要：我们补全了正确性证明并增加了故障注入实验，内容超过二十个字。",
            "response": "感谢评审意见，我们逐条补充了证明，并新增了三组对照实验。",
        }
        payload.update(overrides)
        return payload

    def _submit_revision(self):
        return self.store.submit_revision("alice", self.paper_id, **self._revision_payload())

    def test_full_revision_redeliberation_flow(self):
        # 决定后 7 天内提交，截止时间正好是决定时间 + 7 天。
        decision_at = datetime.fromisoformat(self.clock())
        self.clock.advance(days=6)
        result = self._submit_revision()
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["status"], "under_review")
        self.assertEqual(
            result["deadline"], (decision_at + timedelta(days=7)).isoformat(timespec="seconds")
        )
        # 论文回到评审中，主视图展示新版内容与新版号。
        paper = self.store.get_paper("alice", self.paper_id)
        self.assertEqual(paper["status"], "under_review")
        self.assertEqual(paper["title"], "新版标题：一致性协议修订版")
        self.assertEqual(paper["current_version"], 2)

        # 旧版继续可查：作者和主席能看到旧标题与旧摘要。
        versions = self.store.list_versions("alice", self.paper_id)
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertIn("旧版标题", versions[0]["title"])
        self.assertIn("新版标题", versions[1]["title"])

        status = self.store.revision_status("chair", self.paper_id)
        self.assertEqual(status["expected_reviews"], 2)
        self.assertEqual(status["completed_reviews"], 0)
        self.assertFalse(status["chair_can_redecide"])
        self.assertIsNotNone(status["revision"]["point_by_point"])

        # 只完成一份新版意见时不能改判。
        self.store.submit_revision_review("r1", self.paper_id, 4, "漏洞已补上，证明清晰，可以接收。")
        status = self.store.revision_status("chair", self.paper_id)
        self.assertEqual(status["completed_reviews"], 1)
        self.assertFalse(status["chair_can_redecide"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.redecide("chair", self.paper_id, "accept", "两位评审人均已认可。")
        self.assertEqual(ctx.exception.code, "revision_reviews_incomplete")

        self.store.submit_revision_review("r2", self.paper_id, 4, "实验补充到位，结论可信。")
        status = self.store.revision_status("chair", self.paper_id)
        self.assertTrue(status["chair_can_redecide"])

        # 原有意见只留档：改判门槛只数 revision_reviews，原 assignments 仍为 completed。
        final = self.store.redecide("chair", self.paper_id, "accept", "两位评审人均已认可。")
        self.assertEqual(final["decision"], "accept")
        self.assertEqual(final["round"], 2)
        self.assertEqual(self.store.get_paper("chair", self.paper_id)["status"], "decided")

        history = self.store.history("chair", self.paper_id)
        actions = [item["action"] for item in history]
        self.assertIn("revision.submit", actions)
        self.assertIn("revision_review.submit", actions)
        self.assertEqual(actions[-1], "decision.revise")

    def test_revision_at_exact_deadline_is_allowed(self):
        decision_at = datetime.fromisoformat(self.clock())
        self.clock.moment = decision_at + timedelta(days=7)
        result = self._submit_revision()
        self.assertEqual(result["version"], 2)

    def test_duplicate_revision_is_conflict(self):
        self.clock.advance(days=1)
        self._submit_revision()
        with self.assertRaises(BusinessError) as ctx:
            self._submit_revision()
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "revision_exists")

    def test_late_revision_is_conflict(self):
        self.clock.advance(days=7, seconds=1)
        with self.assertRaises(BusinessError) as ctx:
            self._submit_revision()
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "revision_window_closed")

    def test_revision_only_offered_after_revision_decisions(self):
        paper_id = self.store.submit_paper(
            "bob", "另一篇论文", "另一篇论文的摘要，用于验证直接录用后不能返修，二十字以上。"
        )["id"]
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 5, "质量很好，无需修改，建议直接录用。")
        self.store.submit_review("r2", a2, 5, "完全达到接收标准，没有保留意见。")
        self.store.decide("chair", paper_id, "accept")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision(
                "bob", paper_id, "新版标题", "新版摘要" + "补充内容" * 5, "逐条回复" + "内容" * 4
            )
        self.assertEqual(ctx.exception.code, "revision_not_offered")

    def test_only_original_reviewers_may_re_review_and_no_duplicates(self):
        self._submit_revision()
        # 未参与原审的评审人不能针对新版提意见。
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision_review("r3", self.paper_id, 5, "我是新来的评审，觉得不错。")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "not_original_reviewer")
        self.store.submit_revision_review("r1", self.paper_id, 4, "漏洞已补上，证明清晰。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision_review("r1", self.paper_id, 5, "我想改一下评分和意见。")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "revision_review_exists")

    def test_review_before_revision_and_actions_after_redeliberation_are_conflicts(self):
        # 返修稿未提交时不能复审。
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision_review("r1", self.paper_id, 4, "新版看起来已经修复问题。")
        self.assertEqual(ctx.exception.code, "revision_missing")

        self._submit_revision()
        self.store.submit_revision_review("r1", self.paper_id, 4, "漏洞已补上，证明清晰。")
        self.store.submit_revision_review("r2", self.paper_id, 4, "实验补充到位，结论可信。")
        self.store.redecide("chair", self.paper_id, "accept")
        # 改判后重复复审与重复改判均被拒绝。
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision_review("r2", self.paper_id, 3, "改判后我再补充一点保留意见。")
        self.assertEqual(ctx.exception.code, "revision_closed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.redecide("chair", self.paper_id, "reject")
        self.assertEqual(ctx.exception.code, "already_redecided")

    def test_initial_decision_endpoint_rejects_rereviewed_paper(self):
        self._submit_revision()
        # 返修后论文再次处于 under_review，但首轮决定接口不能被用来绕过改判门槛。
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", self.paper_id, "accept")
        self.assertEqual(ctx.exception.code, "paper_decided")

    def test_invalid_revision_payload(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision("alice", self.paper_id, **self._revision_payload(response="太短"))
        self.assertEqual(ctx.exception.code, "response_too_short")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_revision(
                "alice", self.paper_id, **self._revision_payload(abstract="短摘要")
            )
        self.assertEqual(ctx.exception.code, "invalid_paper")

    def test_visibility_and_double_blind_versions(self):
        self._submit_revision()
        # 双盲：评审人只能看版本元数据，看不到标题/摘要正文。
        reviewer_versions = self.store.list_versions("r1", self.paper_id)
        self.assertEqual({v["version"] for v in reviewer_versions}, {1, 2})
        self.assertNotIn("title", reviewer_versions[0])

        # 其他作者无权查看；未分配的评审人无权查看。
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_versions("bob", self.paper_id)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.revision_status("r3", self.paper_id)
        self.assertEqual(ctx.exception.status, 403)

        # 评审人视角：其他人身份打码，自己的分数可见。
        self.store.submit_revision_review("r1", self.paper_id, 4, "漏洞已补上，证明清晰。")
        status = self.store.revision_status("r1", self.paper_id)
        by_self = [v for v in status["reviews"] if v["submitted"]][0]
        self.assertEqual(by_self["score"], 4)
        self.assertTrue(all(v["reviewer_id"] is None for v in status["reviews"]))
        self.assertIsNone(status["revision"]["point_by_point"])  # 逐条回复对评审人不可见。


class SchemaMigrationTests(unittest.TestCase):
    """旧库（只有 hash 的版本表、无 round 的决定表）可平滑升级。"""

    def test_legacy_schema_is_migrated(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                load_limit INTEGER NOT NULL DEFAULT 3
            );
            CREATE TABLE papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, author_id TEXT NOT NULL,
                title TEXT NOT NULL, abstract TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted', created_at TEXT NOT NULL
            );
            CREATE TABLE paper_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER NOT NULL,
                version INTEGER NOT NULL, content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE (paper_id, version)
            );
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER NOT NULL UNIQUE,
                decision TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                decided_by TEXT NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO users(id,name,role,load_limit) VALUES('chair','主席','chair',0);
            INSERT INTO users(id,name,role,load_limit) VALUES('alice','作者','author',0);
            INSERT INTO papers(id,author_id,title,abstract,status,created_at)
            VALUES(1,'alice','旧标题','旧摘要内容，长度足够。','decided','2026-08-01T00:00:00+00:00');
            INSERT INTO paper_versions(id,paper_id,version,content_hash,created_at)
            VALUES(1,1,1,'deadbeef','2026-08-01T00:00:00+00:00');
            INSERT INTO decisions(id,paper_id,decision,note,decided_by,created_at)
            VALUES(1,1,'major_revision','请大修','chair','2026-08-20T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()

        store = ReviewStore(db_path)
        store.init_schema()

        versions = store.list_versions("chair", 1)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["title"], "旧标题")  # 正文由 papers 回填
        self.assertEqual(versions[0]["sha256"], "deadbeef")
        with store.connect() as conn:
            decision = conn.execute("SELECT * FROM decisions WHERE paper_id=1").fetchone()
            self.assertEqual(decision["round"], 1)
            self.assertEqual(decision["decision"], "major_revision")


if __name__ == "__main__":
    unittest.main()
