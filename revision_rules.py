"""返修规则：大修/小修决定后的限时返修、针对新版的复审与主席改判。

业务规则集中维护于此，与版本存档（version_archive）和作者页面
（web/revision.html）互不耦合：
- 决定发出后 7 天内，作者只能提交一次返修稿（修订稿 + 逐条回复）；
  逾期或重复提交返回 409。
- 返修稿保存为新版本，旧版仍可通过版本存档查询；论文回到 under_review。
- 原评审人各针对新版提交一份复审意见，原有意见仅作存档，不参与改判。
- 全部原评审人完成复审后，主席才能改判；提前改判返回 409。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from app import BusinessError, VALID_DECISIONS, utcnow
from version_archive import validate_manuscript

# 决定发出后作者可提交返修稿的窗口。
REVISION_WINDOW = timedelta(days=7)
# 触发返修流程的决定。
REVISION_DECISIONS = frozenset({"minor_revision", "major_revision"})
# 逐条回复的最少字数。
MIN_RESPONSE_CHARS = 10

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    round INTEGER NOT NULL DEFAULT 1,
    decision TEXT NOT NULL CHECK (decision IN ('accept','reject','minor_revision','major_revision')),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (paper_id, round)
);
CREATE TABLE IF NOT EXISTS revisions (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    point_by_point TEXT NOT NULL,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    submitted_by TEXT NOT NULL REFERENCES users(id),
    deadline TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revision_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    version INTEGER NOT NULL,
    score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (paper_id, reviewer_id)
);
"""


def migrate_decisions(conn: sqlite3.Connection) -> None:
    """旧 decisions 表没有 round 列（决定可能多轮），按需重建。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    if "round" in cols:
        return
    conn.executescript(
        """
        ALTER TABLE decisions RENAME TO decisions_old;
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL REFERENCES papers(id),
            round INTEGER NOT NULL DEFAULT 1,
            decision TEXT NOT NULL CHECK (decision IN ('accept','reject','minor_revision','major_revision')),
            note TEXT NOT NULL DEFAULT '',
            decided_by TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            UNIQUE (paper_id, round)
        );
        INSERT INTO decisions(id,paper_id,round,decision,note,decided_by,created_at)
        SELECT id, paper_id, 1, decision, note, decided_by, created_at FROM decisions_old;
        DROP TABLE decisions_old;
        """
    )


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class RevisionRulesMixin:
    """供 ReviewStore 继承：返修、复审与改判的领域逻辑。"""

    def _latest_decision(self, conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM decisions WHERE paper_id=? ORDER BY round DESC, id DESC LIMIT 1",
            (paper_id,),
        ).fetchone()

    def _original_reviewers(self, conn: sqlite3.Connection, paper_id: int) -> list[sqlite3.Row]:
        # 改判门槛按“决定前已完成评审”的原评审人计算；拒邀/未完成者不占名额。
        return conn.execute(
            "SELECT reviewer_id FROM assignments WHERE paper_id=? AND status='completed' ORDER BY id",
            (paper_id,),
        ).fetchall()

    def submit_revision(
        self, author_id: str, paper_id: int, title: str, abstract: str, response: str
    ) -> dict:
        title, abstract = validate_manuscript(title, abstract)
        response = response.strip()
        if len(response) < MIN_RESPONSE_CHARS:
            raise BusinessError(f"逐条回复至少 {MIN_RESPONSE_CHARS} 字", 422, "response_too_short")
        with self.connect() as conn:
            author = self._user(conn, author_id)
            self._require(author, "author")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["author_id"] != author_id:
                    raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
                decision = self._latest_decision(conn, paper_id)
                if not decision or decision["decision"] not in REVISION_DECISIONS:
                    raise BusinessError("当前决定不允许提交返修稿", 409, "revision_not_offered")
                if conn.execute("SELECT 1 FROM revisions WHERE paper_id=?", (paper_id,)).fetchone():
                    raise BusinessError("返修稿只能提交一次，请勿重复提交", 409, "revision_exists")
                deadline = _parse_ts(decision["created_at"]) + REVISION_WINDOW
                if _parse_ts(self.clock()) > deadline:
                    raise BusinessError("返修期限已过，不再接受提交", 409, "revision_window_closed")
                conn.execute(
                    "UPDATE papers SET title=?, abstract=?, status='under_review' WHERE id=?",
                    (title, abstract, paper_id),
                )
                snap = self._snapshot_version(conn, paper_id, title, abstract)
                deadline_text = deadline.isoformat(timespec="seconds")
                cur = conn.execute(
                    """INSERT INTO revisions(paper_id,version,title,abstract,point_by_point,
                           decision_id,submitted_by,deadline,submitted_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        paper_id, snap["version"], title, abstract, response,
                        decision["id"], author_id, deadline_text, utcnow(),
                    ),
                )
                self._audit(
                    conn, paper_id, author_id, "revision.submit",
                    {"version": snap["version"], "sha256": snap["sha256"], "deadline": deadline_text},
                )
                return {
                    "paper_id": paper_id,
                    "version": snap["version"],
                    "sha256": snap["sha256"],
                    "status": "under_review",
                    "deadline": deadline_text,
                }
            except Exception:
                conn.rollback()
                raise

    def revision_status(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] == "author" and paper["author_id"] != user_id:
                raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
            if user["role"] == "reviewer" and not conn.execute(
                "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? UNION "
                "SELECT 1 FROM revision_reviews WHERE paper_id=? AND reviewer_id=?",
                (paper_id, user_id, paper_id, user_id),
            ).fetchone():
                raise BusinessError("评审人未获授权查看该论文", 403, "forbidden")
            return self._revision_status_view(conn, paper, user)

    def _revision_status_view(self, conn: sqlite3.Connection, paper: sqlite3.Row, viewer: sqlite3.Row) -> dict:
        paper_id = paper["id"]
        decision = self._latest_decision(conn, paper_id)
        revision = conn.execute("SELECT * FROM revisions WHERE paper_id=?", (paper_id,)).fetchone()
        original = self._original_reviewers(conn, paper_id)
        rows = conn.execute(
            "SELECT reviewer_id, score, created_at FROM revision_reviews WHERE paper_id=? ORDER BY id",
            (paper_id,),
        ).fetchall()
        reviews_by_reviewer = {row["reviewer_id"]: row for row in rows}
        reviews = []
        for assignment in original:
            reviewer_id = assignment["reviewer_id"]
            done = reviewer_id in reviews_by_reviewer
            item: dict = {"reviewer_id": reviewer_id if viewer["role"] == "chair" else None,
                          "submitted": done}
            if viewer["role"] == "chair" and done:
                item["score"] = reviews_by_reviewer[reviewer_id]["score"]
                item["submitted_at"] = reviews_by_reviewer[reviewer_id]["created_at"]
            if viewer["role"] == "reviewer" and reviewer_id == viewer["id"] and done:
                item["score"] = reviews_by_reviewer[reviewer_id]["score"]
            reviews.append(item)
        expected = len(original)
        completed = len(rows)
        data: dict = {
            "paper_id": paper_id,
            "status": paper["status"],
            "current_version": self._latest_version_number(conn, paper_id),
            "latest_decision": dict(decision) if decision else None,
            "revision": None,
            "expected_reviews": expected,
            "completed_reviews": completed,
            "chair_can_redecide": expected > 0 and completed == expected and paper["status"] == "under_review",
            "reviews": reviews,
        }
        if revision:
            # 逐条回复仅作者与主席可见；评审人只知道新版号。
            data["revision"] = {
                "version": revision["version"],
                "submitted_at": revision["submitted_at"],
                "deadline": revision["deadline"],
                "point_by_point": revision["point_by_point"] if viewer["role"] in {"chair", "author"} else None,
            }
        return data

    def submit_revision_review(self, reviewer_id: str, paper_id: int, score: int, text: str) -> dict:
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise BusinessError("评分必须是 1 到 5 的整数", 422, "invalid_score")
        if len(text.strip()) < 10:
            raise BusinessError("复审意见至少 10 字", 422, "review_too_short")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            revision = conn.execute("SELECT * FROM revisions WHERE paper_id=?", (paper_id,)).fetchone()
            if not revision:
                raise BusinessError("尚无返修稿，不能提交复审", 409, "revision_missing")
            if paper["status"] != "under_review":
                raise BusinessError("复审已截止（主席已改判）", 409, "revision_closed")
            original = conn.execute(
                "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? AND status='completed'",
                (paper_id, reviewer_id),
            ).fetchone()
            if not original:
                raise BusinessError("只有原评审人可以针对新版提交意见", 403, "not_original_reviewer")
            try:
                cur = conn.execute(
                    "INSERT INTO revision_reviews(paper_id,reviewer_id,version,score,text,created_at) VALUES(?,?,?,?,?,?)",
                    (paper_id, reviewer_id, revision["version"], score, text.strip(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("每位原评审人只能提交一份新版意见，请勿重复提交", 409, "revision_review_exists")
            self._audit(
                conn, paper_id, reviewer_id, "revision_review.submit",
                {"revision_review_id": cur.lastrowid, "version": revision["version"], "score": score},
            )
            return {"id": cur.lastrowid, "paper_id": paper_id, "version": revision["version"], "score": score}

    def redecide(self, chair_id: str, paper_id: int, decision: str, note: str = "") -> dict:
        if decision not in VALID_DECISIONS:
            raise BusinessError("决定值不合法", 422, "invalid_decision")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper:
                    raise BusinessError("论文不存在", 404, "not_found")
                revision = conn.execute("SELECT * FROM revisions WHERE paper_id=?", (paper_id,)).fetchone()
                if not revision:
                    raise BusinessError("尚无返修稿，不能改判", 409, "revision_missing")
                if paper["status"] != "under_review":
                    raise BusinessError("主席已经改判，请勿重复操作", 409, "already_redecided")
                expected = len(self._original_reviewers(conn, paper_id))
                completed = conn.execute(
                    "SELECT COUNT(*) FROM revision_reviews WHERE paper_id=?", (paper_id,)
                ).fetchone()[0]
                if expected == 0 or completed < expected:
                    raise BusinessError("全部原评审人完成新版意见后才能改判", 409, "revision_reviews_incomplete")
                latest = self._latest_decision(conn, paper_id)
                next_round = (latest["round"] if latest else 0) + 1
                cur = conn.execute(
                    "INSERT INTO decisions(paper_id,round,decision,note,decided_by,created_at) VALUES(?,?,?,?,?,?)",
                    (paper_id, next_round, decision, note.strip(), chair_id, utcnow()),
                )
                conn.execute("UPDATE papers SET status='decided' WHERE id=?", (paper_id,))
                self._audit(
                    conn, paper_id, chair_id, "decision.revise",
                    {"decision": decision, "round": next_round, "note": note.strip()},
                )
                return {
                    "id": cur.lastrowid,
                    "paper_id": paper_id,
                    "round": next_round,
                    "decision": decision,
                    "note": note.strip(),
                }
            except Exception:
                conn.rollback()
                raise
