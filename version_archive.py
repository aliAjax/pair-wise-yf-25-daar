"""版本存档：论文内容快照的写入与查询，与评审流程解耦，旧版本永久可查。"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(title: str, abstract: str) -> str:
    return hashlib.sha256(f"{title}\n{abstract}".encode()).hexdigest()


class VersionArchive:
    """按 (paper_id, version) 保存不可变快照。"""

    @staticmethod
    def snapshot(conn: sqlite3.Connection, paper_id: int, title: str, abstract: str) -> dict:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM paper_versions WHERE paper_id=?", (paper_id,)
        ).fetchone()
        version = row["v"] + 1
        digest = content_hash(title, abstract)
        conn.execute(
            "INSERT INTO paper_versions(paper_id,version,title,abstract,content_hash,created_at) VALUES(?,?,?,?,?,?)",
            (paper_id, version, title, abstract, digest, _utcnow()),
        )
        return {"paper_id": paper_id, "version": version, "sha256": digest}

    @staticmethod
    def list_versions(conn: sqlite3.Connection, paper_id: int) -> list[dict]:
        rows = conn.execute(
            "SELECT version,title,abstract,content_hash,created_at FROM paper_versions WHERE paper_id=? ORDER BY version",
            (paper_id,),
        ).fetchall()
        return [dict(row) for row in rows]
