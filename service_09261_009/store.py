"""SQLite 持久化仓储：版本、差异、逐行归属与发布快照分表固化。

写入在单事务内完成；发布快照自带 body 副本与 SHA-256，版本正文随后被
修改或新增草稿都不会影响已发布内容。
"""
import json
import sqlite3
from dataclasses import asdict

from .workflow import (
    BlameRecord,
    Conflict,
    DiffRecord,
    Publication,
    VersionRecord,
)


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self._schema()

    def _schema(self):
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents(
              doc_id TEXT PRIMARY KEY, actor TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS versions(
              doc_id TEXT NOT NULL, version INTEGER NOT NULL,
              actor TEXT NOT NULL, parent INTEGER,
              body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
              event_time TEXT NOT NULL, recorded_time TEXT NOT NULL,
              PRIMARY KEY(doc_id, version)
            );
            CREATE TABLE IF NOT EXISTS diffs(
              doc_id TEXT NOT NULL, version INTEGER NOT NULL, seq INTEGER NOT NULL,
              tag TEXT NOT NULL, actor TEXT NOT NULL,
              old_start INTEGER NOT NULL, old_end INTEGER NOT NULL,
              new_start INTEGER NOT NULL, new_end INTEGER NOT NULL,
              old_lines TEXT NOT NULL, new_lines TEXT NOT NULL,
              PRIMARY KEY(doc_id, version, seq)
            );
            CREATE TABLE IF NOT EXISTS blame(
              doc_id TEXT NOT NULL, version INTEGER NOT NULL, ordinal INTEGER NOT NULL,
              line_sha256 TEXT NOT NULL, introduced_version INTEGER NOT NULL,
              actor TEXT NOT NULL, introduced_at TEXT NOT NULL,
              PRIMARY KEY(doc_id, version, ordinal)
            );
            CREATE TABLE IF NOT EXISTS publications(
              doc_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
              body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
              actor TEXT NOT NULL, event_time TEXT NOT NULL, recorded_time TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS idem(
              key TEXT PRIMARY KEY, doc_id TEXT NOT NULL,
              version INTEGER NOT NULL, kind TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    # ---------- 文档 / 版本 ----------

    def create_document(self, doc_id, actor, created_at):
        try:
            self.db.execute(
                "INSERT INTO documents(doc_id,actor,created_at) VALUES(?,?,?)",
                (doc_id, actor, created_at))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise Conflict(str(exc)) from exc

    def document_exists(self, doc_id):
        return self.db.execute(
            "SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone() \
            is not None

    def head_version_number(self, doc_id):
        row = self.db.execute(
            "SELECT MAX(version) FROM versions WHERE doc_id=?",
            (doc_id,)).fetchone()
        return row[0]

    def insert_version(self, version, diffs, blame):
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO versions(doc_id,version,actor,parent,body,"
                    "body_sha256,event_time,recorded_time) VALUES(?,?,?,?,?,?,?,?)",
                    (version.doc_id, version.version, version.actor,
                     version.parent, version.body, version.body_sha256,
                     version.event_time, version.recorded_time))
                self.db.executemany(
                    "INSERT INTO diffs(doc_id,version,seq,tag,actor,old_start,"
                    "old_end,new_start,new_end,old_lines,new_lines)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [(version.doc_id, version.version, d.seq, d.tag, d.actor,
                      d.old_start, d.old_end, d.new_start, d.new_end,
                      json.dumps(d.old_lines, ensure_ascii=False),
                      json.dumps(d.new_lines, ensure_ascii=False))
                     for d in diffs])
                self.db.executemany(
                    "INSERT INTO blame(doc_id,version,ordinal,line_sha256,"
                    "introduced_version,actor,introduced_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    [(b.doc_id if hasattr(b, "doc_id") else version.doc_id,
                      version.version, b.ordinal, b.line_sha256,
                      b.introduced_version, b.actor, b.introduced_at)
                     for b in blame])
        except sqlite3.IntegrityError as exc:
            raise Conflict(str(exc)) from exc

    def get_version(self, doc_id, number):
        row = self.db.execute(
            "SELECT doc_id,version,actor,parent,body,body_sha256,event_time,"
            "recorded_time FROM versions WHERE doc_id=? AND version=?",
            (doc_id, number)).fetchone()
        return VersionRecord(*row) if row else None

    def list_versions(self, doc_id):
        rows = self.db.execute(
            "SELECT doc_id,version,actor,parent,body,body_sha256,event_time,"
            "recorded_time FROM versions WHERE doc_id=? ORDER BY version",
            (doc_id,)).fetchall()
        return [VersionRecord(*r) for r in rows]

    def get_diffs(self, doc_id, number):
        rows = self.db.execute(
            "SELECT seq,tag,actor,old_start,old_end,new_start,new_end,"
            "old_lines,new_lines FROM diffs WHERE doc_id=? AND version=?"
            " ORDER BY seq", (doc_id, number)).fetchall()
        return [DiffRecord(seq=r[0], tag=r[1], actor=r[2], old_start=r[3],
                           old_end=r[4], new_start=r[5], new_end=r[6],
                           old_lines=json.loads(r[7]),
                           new_lines=json.loads(r[8])) for r in rows]

    def get_blame(self, doc_id, number):
        rows = self.db.execute(
            "SELECT ordinal,line_sha256,introduced_version,actor,introduced_at"
            " FROM blame WHERE doc_id=? AND version=? ORDER BY ordinal",
            (doc_id, number)).fetchall()
        return [BlameRecord(ordinal=r[0], line_sha256=r[1],
                            introduced_version=r[2], actor=r[3],
                            introduced_at=r[4]) for r in rows]

    # ---------- 发布 ----------

    def mark_published(self, publication):
        self.db.execute(
            "INSERT INTO publications(doc_id,version,body,body_sha256,actor,"
            "event_time,recorded_time) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(doc_id) DO UPDATE SET version=excluded.version,"
            "body=excluded.body,body_sha256=excluded.body_sha256,"
            "actor=excluded.actor,event_time=excluded.event_time,"
            "recorded_time=excluded.recorded_time",
            (publication.doc_id, publication.version, publication.body,
             publication.body_sha256, publication.actor,
             publication.event_time, publication.recorded_time))
        self.db.commit()

    def get_current_publication(self, doc_id):
        r = self.db.execute(
            "SELECT doc_id,version,body,body_sha256,actor,event_time,"
            "recorded_time FROM publications WHERE doc_id=?",
            (doc_id,)).fetchone()
        return Publication(*r) if r else None

    # ---------- 幂等 ----------

    def put_idem(self, key, replay):
        if isinstance(replay, Publication):
            kind = "publication"
            doc_id, number = replay.doc_id, replay.version
        else:
            kind = "version"
            doc_id, number = replay.doc_id, replay.version
        try:
            self.db.execute(
                "INSERT INTO idem(key,doc_id,version,kind) VALUES(?,?,?,?)",
                (key, doc_id, number, kind))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise Conflict(str(exc)) from exc

    def get_idem(self, key):
        row = self.db.execute(
            "SELECT doc_id,version,kind FROM idem WHERE key=?",
            (key,)).fetchone()
        if row is None:
            return None
        doc_id, number, kind = row
        if kind == "publication":
            return self.get_current_publication(doc_id)
        return self.get_version(doc_id, number)
