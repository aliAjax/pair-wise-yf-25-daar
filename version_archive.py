"""版本存档：论文每一次提交（初投与返修稿）都在此保存可查的历史快照。

与返修规则（revision_rules）、作者页面（web/revision.html）分开维护。
"""
from __future__ import annotations

import hashlib
import sqlite3

from app import BusinessError, utcnow

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS paper_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (paper_id, version)
);
"""

# 初投稿即第 1 版；返修稿在此基础上递增。
INITIAL_VERSION = 1


def migrate(conn: sqlite3.Connection) -> None:
    """把仅含 content_hash 的旧版 paper_versions 升级为全文快照。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(paper_versions)").fetchall()}
    if not cols or {"title", "abstract"} <= cols:
        return
    conn.executescript(
        """
        ALTER TABLE paper_versions RENAME TO paper_versions_old;
        CREATE TABLE paper_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL REFERENCES papers(id),
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            abstract TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (paper_id, version)
        );
        INSERT INTO paper_versions(id,paper_id,version,title,abstract,content_hash,created_at)
        SELECT v.id, v.paper_id, v.version, p.title, p.abstract, v.content_hash, v.created_at
        FROM paper_versions_old v JOIN papers p ON p.id = v.paper_id;
        DROP TABLE paper_versions_old;
        """
    )


def digest(title: str, abstract: str) -> str:
    return hashlib.sha256(f"{title}\n{abstract}".encode()).hexdigest()


def validate_manuscript(title: str, abstract: str) -> tuple[str, str]:
    title, abstract = title.strip(), abstract.strip()
    if len(title) < 3 or len(abstract) < 20:
        raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
    return title, abstract


class VersionArchiveMixin:
    """供 ReviewStore 继承：版本快照的写入与查询。"""

    def _snapshot_version(self, conn: sqlite3.Connection, paper_id: int, title: str, abstract: str) -> dict:
        """写入下一版快照并返回版本信息。"""
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM paper_versions WHERE paper_id=?",
            (paper_id,),
        ).fetchone()
        version = row["next"]
        content_hash = digest(title, abstract)
        conn.execute(
            "INSERT INTO paper_versions(paper_id,version,title,abstract,content_hash,created_at) VALUES(?,?,?,?,?,?)",
            (paper_id, version, title, abstract, content_hash, utcnow()),
        )
        return {"version": version, "sha256": content_hash}

    def _latest_version_number(self, conn: sqlite3.Connection, paper_id: int) -> int:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM paper_versions WHERE paper_id=?", (paper_id,)
        ).fetchone()
        return row["v"] or INITIAL_VERSION

    def list_versions(self, user_id: str, paper_id: int) -> list[dict]:
        """旧版与新版都可查；正文仅作者本人与主席可见，评审人只看到版本元数据（双盲）。"""
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] == "reviewer":
                allowed = conn.execute(
                    "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? UNION "
                    "SELECT 1 FROM revision_reviews WHERE paper_id=? AND reviewer_id=?",
                    (paper_id, user_id, paper_id, user_id),
                ).fetchone()
                if not allowed:
                    raise BusinessError("评审人未获授权查看该论文", 403, "forbidden")
            elif user["role"] == "author" and paper["author_id"] != user_id:
                raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
            rows = conn.execute(
                "SELECT * FROM paper_versions WHERE paper_id=? ORDER BY version", (paper_id,)
            ).fetchall()
            can_read_body = user["role"] == "chair" or paper["author_id"] == user_id
            versions = []
            for row in rows:
                item = {
                    "version": row["version"],
                    "sha256": row["content_hash"],
                    "created_at": row["created_at"],
                }
                if can_read_body:
                    item["title"] = row["title"]
                    item["abstract"] = row["abstract"]
                versions.append(item)
            return versions
