"""SQLite 仓储：版本与发布均为只插入（insert-only），历史行永不更新。"""

import json
import sqlite3

from . import textbook


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS versions(
              doc_id       TEXT NOT NULL,
              version_no   INTEGER NOT NULL,
              parent_no    INTEGER,
              actor        TEXT NOT NULL,
              content      TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              ops          TEXT NOT NULL,
              diff_hash    TEXT NOT NULL,
              checksum     TEXT NOT NULL,
              event_time   TEXT NOT NULL,
              record_time  TEXT NOT NULL,
              PRIMARY KEY(doc_id, version_no)
            );
            CREATE TABLE IF NOT EXISTS publications(
              doc_id       TEXT NOT NULL,
              edition_no   INTEGER NOT NULL,
              version_no   INTEGER NOT NULL,
              actor        TEXT NOT NULL,
              content      TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              checksum     TEXT NOT NULL,
              event_time   TEXT NOT NULL,
              record_time  TEXT NOT NULL,
              PRIMARY KEY(doc_id, edition_no)
            );
            CREATE TABLE IF NOT EXISTS idempotency_keys(
              scope TEXT NOT NULL,
              idem_key TEXT NOT NULL,
              doc_id TEXT NOT NULL,
              version_no INTEGER NOT NULL,
              PRIMARY KEY(scope, idem_key)
            );
            """
        )
        self.db.commit()

    # ---------- 版本 ----------

    def put_version(self, rec):
        self.db.execute(
            "INSERT INTO versions(doc_id,version_no,parent_no,actor,content,"
            "content_hash,ops,diff_hash,checksum,event_time,record_time) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec["doc_id"], rec["version_no"], rec["parent_no"],
                rec["actor"], rec["content"], rec["content_hash"],
                json.dumps(rec["ops"], ensure_ascii=False), rec["diff_hash"],
                rec["checksum"], rec["event_time"], rec["record_time"],
            ),
        )
        self.db.commit()

    def get_version(self, doc_id, version_no):
        row = self.db.execute(
            "SELECT doc_id,version_no,parent_no,actor,content,content_hash,"
            "ops,diff_hash,checksum,event_time,record_time "
            "FROM versions WHERE doc_id=? AND version_no=?",
            (doc_id, version_no),
        ).fetchone()
        return None if row is None else self._version_row(row)

    def list_versions(self, doc_id):
        rows = self.db.execute(
            "SELECT doc_id,version_no,parent_no,actor,content,content_hash,"
            "ops,diff_hash,checksum,event_time,record_time "
            "FROM versions WHERE doc_id=? ORDER BY version_no",
            (doc_id,),
        ).fetchall()
        return [self._version_row(r) for r in rows]

    def latest_version_no(self, doc_id):
        row = self.db.execute(
            "SELECT MAX(version_no) FROM versions WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return row[0]

    @staticmethod
    def _version_row(row):
        return {
            "doc_id": row[0], "version_no": row[1], "parent_no": row[2],
            "actor": row[3], "content": row[4], "content_hash": row[5],
            "ops": json.loads(row[6]), "diff_hash": row[7],
            "checksum": row[8], "event_time": row[9], "record_time": row[10],
        }

    # ---------- 发布 ----------

    def put_publication(self, rec):
        self.db.execute(
            "INSERT INTO publications(doc_id,edition_no,version_no,actor,"
            "content,content_hash,checksum,event_time,record_time) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                rec["doc_id"], rec["edition_no"], rec["version_no"],
                rec["actor"], rec["content"], rec["content_hash"],
                rec["checksum"], rec["event_time"], rec["record_time"],
            ),
        )
        self.db.commit()

    def get_publication(self, doc_id, edition_no=None):
        if edition_no is None:
            sql = (
                "SELECT doc_id,edition_no,version_no,actor,content,"
                "content_hash,checksum,event_time,record_time "
                "FROM publications WHERE doc_id=? ORDER BY edition_no DESC LIMIT 1"
            )
            row = self.db.execute(sql, (doc_id,)).fetchone()
        else:
            sql = (
                "SELECT doc_id,edition_no,version_no,actor,content,"
                "content_hash,checksum,event_time,record_time "
                "FROM publications WHERE doc_id=? AND edition_no=?"
            )
            row = self.db.execute(sql, (doc_id, edition_no)).fetchone()
        return None if row is None else self._publication_row(row)

    def list_publications(self, doc_id):
        rows = self.db.execute(
            "SELECT doc_id,edition_no,version_no,actor,content,content_hash,"
            "checksum,event_time,record_time FROM publications "
            "WHERE doc_id=? ORDER BY edition_no",
            (doc_id,),
        ).fetchall()
        return [self._publication_row(r) for r in rows]

    @staticmethod
    def _publication_row(row):
        return {
            "doc_id": row[0], "edition_no": row[1], "version_no": row[2],
            "actor": row[3], "content": row[4], "content_hash": row[5],
            "checksum": row[6], "event_time": row[7], "record_time": row[8],
        }

    # ---------- 幂等键 ----------

    def store_idempotency(self, scope, key, doc_id, version_no):
        self.db.execute(
            "INSERT OR IGNORE INTO idempotency_keys(scope,idem_key,doc_id,version_no)"
            " VALUES(?,?,?,?)",
            (scope, key, doc_id, version_no),
        )
        self.db.commit()

    def lookup_idempotency(self, scope, key):
        row = self.db.execute(
            "SELECT doc_id,version_no FROM idempotency_keys WHERE scope=? AND idem_key=?",
            (scope, key),
        ).fetchone()
        return None if row is None else {"doc_id": row[0], "version_no": row[1]}

    # ---------- 独立校验：不信任任何已存哈希，全部复算 ----------

    def verify(self, doc_id):
        result = {"ok": True, "errors": [], "versions_checked": 0,
                  "publications_checked": 0}
        versions = self.list_versions(doc_id)
        by_no = {v["version_no"]: v for v in versions}
        prev_checksum = ""
        for v in versions:
            result["versions_checked"] += 1
            no = v["version_no"]
            # 内容哈希复算
            if textbook.content_digest(v["content"]) != v["content_hash"]:
                result["ok"] = False
                result["errors"].append(f"v{no}: content_hash mismatch")
            # 差异必须相对真实父版内容重新生成后一致
            parent = None if v["parent_no"] is None else by_no.get(v["parent_no"])
            if v["parent_no"] is not None and parent is None:
                result["ok"] = False
                result["errors"].append(f"v{no}: missing parent v{v['parent_no']}")
            elif v["parent_no"] is None:
                # 根版本没有父版，差异按定义为空，不与 diff("", content) 比较
                if v["ops"] != []:
                    result["ok"] = False
                    result["errors"].append(f"v{no}: root ops must be empty")
                if textbook.diff_digest([]) != v["diff_hash"]:
                    result["ok"] = False
                    result["errors"].append(f"v{no}: diff_hash mismatch")
            else:
                ops = textbook.diff_opcodes(parent["content"], v["content"])
                if textbook.canonical(ops) != textbook.canonical(v["ops"]):
                    result["ok"] = False
                    result["errors"].append(f"v{no}: diff ops mismatch")
                if textbook.diff_digest(ops) != v["diff_hash"]:
                    result["ok"] = False
                    result["errors"].append(f"v{no}: diff_hash mismatch")
            expected = textbook.record_checksum(
                v["doc_id"], no, v["parent_no"], v["actor"],
                v["content_hash"], v["diff_hash"], v["event_time"],
                v["record_time"], prev_checksum,
            )
            if expected != v["checksum"]:
                result["ok"] = False
                result["errors"].append(f"v{no}: checksum chain mismatch")
            prev_checksum = v["checksum"]

        prev_pub = ""
        for p in self.list_publications(doc_id):
            result["publications_checked"] += 1
            v = by_no.get(p["version_no"])
            if v is None:
                result["ok"] = False
                result["errors"].append(f"edition {p['edition_no']}: missing version")
            elif v["content"] != p["content"]:
                result["ok"] = False
                result["errors"].append(
                    f"edition {p['edition_no']}: frozen content differs from v{p['version_no']}"
                )
            if textbook.content_digest(p["content"]) != p["content_hash"]:
                result["ok"] = False
                result["errors"].append(
                    f"edition {p['edition_no']}: content_hash mismatch"
                )
            expected = textbook.publication_checksum(
                p["doc_id"], p["edition_no"], p["version_no"], p["content_hash"],
                p["actor"], p["event_time"], p["record_time"], prev_pub,
            )
            if expected != p["checksum"]:
                result["ok"] = False
                result["errors"].append(
                    f"edition {p['edition_no']}: checksum chain mismatch"
                )
            prev_pub = p["checksum"]
        return result
