"""教材共创：版本差异、逐行贡献归属与发布固定。

设计要点：
- 每次提交保存完整文本、相对上一版的差异（opcode 级，提交时固化）、
  以及与新版本行对齐的归属（blame）记录。
- 未改动的行沿用上一版归属；新增/替换行归属本次提交人，因此整稿覆盖
  也不会抹掉原作者。
- 发布动作对当时采用的整稿内容做快照（含 SHA-256），之后的新草稿、
- 被废弃版本的内容都不会再混入默认查询与汇总。
- 归属记录显式携带 introduced_version 与行内容校验值；跨天补录只影响
  event_time，不会让记录被错误归到更早的版本。
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Optional


class NotFound(Exception):
    """文档或版本不存在。"""


class Conflict(Exception):
    """与当前状态冲突（如重复创建）。"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_time(value, default: datetime) -> str:
    """把 datetime / ISO 字符串统一成 UTC ISO 字符串；缺省用 default。"""
    if value is None:
        value = default
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError("event_time 必须是 ISO 字符串或 datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def split_lines(body: str) -> list[str]:
    return body.splitlines()


@dataclass(frozen=True)
class VersionRecord:
    doc_id: str
    version: int
    actor: str
    parent: Optional[int]
    body: str
    body_sha256: str
    event_time: str       # 声称的贡献时间（可跨天补录）
    recorded_time: str    # 实际入库时间，服务端时钟


@dataclass(frozen=True)
class DiffRecord:
    """相对上一版的一段差异，提交时固化。

    行号为 0 基半开区间；tag 取 difflib opcode：
    equal / insert / replace / delete。
    """
    seq: int
    tag: str
    actor: str
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    old_lines: list[str]
    new_lines: list[str]


@dataclass(frozen=True)
class BlameRecord:
    """新版本中某一行的归属，ordinal 从 1 开始、与正文行对齐。"""
    ordinal: int
    line_sha256: str
    introduced_version: int
    actor: str
    introduced_at: str


@dataclass(frozen=True)
class Publication:
    """一次发布所固定的内容快照。"""
    doc_id: str
    version: int
    body: str
    body_sha256: str
    actor: str
    event_time: str
    recorded_time: str


def compute_diff_and_blame(old_lines, new_lines, parent_blame,
                           version, actor, event_time):
    """基于 difflib opcode 同时生成差异记录与新行归属。"""
    matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    diffs: list[DiffRecord] = []
    claimed: list[BlameRecord] = []
    for seq, (tag, i1, i2, j1, j2) in enumerate(matcher.get_opcodes()):
        diffs.append(DiffRecord(
            seq=seq, tag=tag, actor=actor,
            old_start=i1, old_end=i2, new_start=j1, new_end=j2,
            old_lines=old_lines[i1:i2], new_lines=new_lines[j1:j2],
        ))
        if tag == "equal":
            # 未改动的行：完整继承上一版归属（含作者、引入版本、时间）
            for k in range(i1, i2):
                claimed.append(parent_blame[k])
        else:
            # insert / replace 产生的新行归本次提交人；delete 不产生新行
            for j in range(j1, j2):
                claimed.append(BlameRecord(
                    ordinal=0,
                    line_sha256=sha256_text(new_lines[j]),
                    introduced_version=version,
                    actor=actor,
                    introduced_at=event_time,
                ))
    # 按新正文重新编号
    blame = [BlameRecord(ordinal=i + 1, line_sha256=b.line_sha256,
                         introduced_version=b.introduced_version,
                         actor=b.actor, introduced_at=b.introduced_at)
             for i, b in enumerate(claimed)]
    return diffs, blame


class MemoryRepo:
    """Workflow 依赖的仓储接口的内存实现。"""

    def __init__(self):
        self._docs = {}
        self._versions = {}
        self._diffs = {}
        self._blame = {}
        self._publications = {}
        self._idem = {}

    def create_document(self, doc_id, actor, created_at):
        if doc_id in self._docs:
            raise Conflict("document already exists")
        self._docs[doc_id] = {"actor": actor, "created_at": created_at}

    def document_exists(self, doc_id):
        return doc_id in self._docs

    def head_version_number(self, doc_id):
        numbers = [v for (d, v) in self._versions if d == doc_id]
        return max(numbers) if numbers else None

    def insert_version(self, version, diffs, blame):
        key = (version.doc_id, version.version)
        if key in self._versions:
            raise Conflict("version already exists")
        self._versions[key] = version
        self._diffs[key] = list(diffs)
        self._blame[key] = list(blame)

    def get_version(self, doc_id, number):
        return self._versions.get((doc_id, number))

    def list_versions(self, doc_id):
        return [self._versions[k] for k in sorted(self._versions)
                if k[0] == doc_id]

    def get_diffs(self, doc_id, number):
        return list(self._diffs.get((doc_id, number), ()))

    def get_blame(self, doc_id, number):
        return list(self._blame.get((doc_id, number), ()))

    def put_idem(self, key, replay):
        if key in self._idem:
            raise Conflict("idempotency key already used")
        self._idem[key] = replay

    def get_idem(self, key):
        return self._idem.get(key)

    def mark_published(self, publication):
        self._publications[publication.doc_id] = publication

    def get_current_publication(self, doc_id):
        return self._publications.get(doc_id)


class Workflow:
    def __init__(self, repo=None, clock=None):
        self.repo = repo or MemoryRepo()
        self.clock = clock or utcnow

    # ---------- 写命令 ----------

    def create_document(self, doc_id, actor, body="", event_time=None,
                        idempotency_key=None):
        hit = self._idem_hit(idempotency_key)
        if hit is not None:
            return hit
        if self.repo.document_exists(doc_id):
            raise Conflict(f"document {doc_id!r} already exists")
        recorded = self.clock()
        event_iso = normalize_time(event_time, recorded)
        self.repo.create_document(doc_id, actor, event_iso)
        version, diffs, blame = self._build(
            doc_id, actor, body, None, event_iso, recorded)
        self.repo.insert_version(version, diffs, blame)
        if idempotency_key:
            self.repo.put_idem(idempotency_key, version)
        return version

    def commit(self, doc_id, actor, body, event_time=None,
               idempotency_key=None):
        """提交新稿（允许直接覆盖旧稿）；差异与归属在此刻固化。"""
        hit = self._idem_hit(idempotency_key)
        if hit is not None:
            return hit
        parent = self._require_head(doc_id)
        recorded = self.clock()
        event_iso = normalize_time(event_time, recorded)
        version, diffs, blame = self._build(
            doc_id, actor, body, parent, event_iso, recorded)
        self.repo.insert_version(version, diffs, blame)
        if idempotency_key:
            self.repo.put_idem(idempotency_key, version)
        return version

    def publish(self, doc_id, actor, version=None, event_time=None,
                idempotency_key=None):
        """发布：固定当时采用的整稿内容（快照 + 校验值）。"""
        hit = self._idem_hit(idempotency_key)
        if hit is not None and isinstance(hit, Publication):
            return hit
        self._require_doc(doc_id)
        target = self.repo.get_version(doc_id, version) if version is not None \
            else self._require_head(doc_id)
        if target is None:
            raise NotFound(f"version {version} not found")
        # 发布前校验待发布内容自身完整
        if sha256_text(target.body) != target.body_sha256:
            raise ValueError("target version checksum mismatch")
        recorded = self.clock()
        publication = Publication(
            doc_id=doc_id, version=target.version, body=target.body,
            body_sha256=target.body_sha256, actor=actor,
            event_time=normalize_time(event_time, recorded),
            recorded_time=recorded.isoformat(),
        )
        self.repo.mark_published(publication)
        if idempotency_key:
            self.repo.put_idem(idempotency_key, publication)
        return publication

    # ---------- 读模型 ----------

    def versions(self, doc_id):
        self._require_doc(doc_id)
        pub = self.repo.get_current_publication(doc_id)
        pub_version = pub.version if pub else None
        head = self.repo.head_version_number(doc_id)
        return [{**asdict(v), "status": self._status(
            v.version, pub_version, head)} for v in
            self.repo.list_versions(doc_id)]

    def version_view(self, doc_id, number):
        v = self._require_version(doc_id, number)
        pub = self.repo.get_current_publication(doc_id)
        head = self.repo.head_version_number(doc_id)
        return {**asdict(v), "status": self._status(
            v.version, pub.version if pub else None, head),
            "verified": self.verify(doc_id, number)}

    def diff_view(self, doc_id, number):
        v = self._require_version(doc_id, number)
        return {"doc_id": doc_id, "version": v.version,
                "parent": v.parent, "actor": v.actor,
                "event_time": v.event_time,
                "records": [asdict(d) for d in
                            self.repo.get_diffs(doc_id, number)]}

    def published_view(self, doc_id):
        pub = self.repo.get_current_publication(doc_id)
        if pub is None:
            raise NotFound(f"document {doc_id!r} has no publication")
        current = self.repo.get_version(doc_id, pub.version)
        # 快照内容自校验 + 与当前版本行比对：被篡改时 verified=False
        verified = (
            sha256_text(pub.body) == pub.body_sha256
            and current is not None
            and current.body_sha256 == pub.body_sha256
            and current.body == pub.body
        )
        return {"doc_id": doc_id, "version": pub.version,
                "body": pub.body, "body_sha256": pub.body_sha256,
                "published_by": pub.actor, "published_at": pub.event_time,
                "recorded_at": pub.recorded_time, "verified": verified}

    def contributions(self, doc_id, actor=None, introduced_from=None,
                      introduced_to=None, version=None):
        """查询某一固定版本中的逐行贡献归属。

        scope 默认 published：只看当前发布版本，未采用的草稿与已被删除的
        历史行都不会混入；显式传 version 则固定到该版本。
        每条记录都带引入版本号与该行内容的 SHA-256。
        """
        pinned, scope = self._pinned(doc_id, version)
        v = self._require_version(doc_id, pinned)
        lines = split_lines(v.body)
        start = self._parse_bound(introduced_from, "introduced_from")
        end = self._parse_bound(introduced_to, "introduced_to")
        records = []
        for b in self.repo.get_blame(doc_id, pinned):
            if actor is not None and b.actor != actor:
                continue
            introduced_at = datetime.fromisoformat(b.introduced_at)
            if start and introduced_at < start:
                continue
            if end and introduced_at > end:
                continue
            line = lines[b.ordinal - 1]
            # 查询时重新校验行内容与归属记录一致
            line_verified = sha256_text(line) == b.line_sha256
            records.append({
                "ordinal": b.ordinal, "actor": b.actor,
                "introduced_version": b.introduced_version,
                "introduced_at": b.introduced_at,
                "line_sha256": b.line_sha256, "line": line,
                "line_verified": line_verified,
            })
        return {"doc_id": doc_id, "scope": scope, "version": pinned,
                "body_sha256": v.body_sha256,
                "verified": self.verify(doc_id, pinned),
                "records": records}

    def summary(self, doc_id, version=None):
        """按作者汇总某固定版本内仍被采用的贡献；过期内容不计入。"""
        pinned, scope = self._pinned(doc_id, version)
        v = self._require_version(doc_id, pinned)
        per_actor = {}
        for b in self.repo.get_blame(doc_id, pinned):
            item = per_actor.setdefault(b.actor, {
                "actor": b.actor, "lines": 0,
                "versions": set(), "line_sha256": set()})
            item["lines"] += 1
            item["versions"].add(b.introduced_version)
            item["line_sha256"].add(b.line_sha256)
        contributors = [{
            "actor": a["actor"], "lines": a["lines"],
            "versions": sorted(a["versions"]),
            "line_sha256": sorted(a["line_sha256"]),
        } for a in sorted(per_actor.values(), key=lambda x: x["actor"])]
        return {"doc_id": doc_id, "scope": scope, "version": pinned,
                "body_sha256": v.body_sha256,
                "verified": self.verify(doc_id, pinned),
                "lines_total": len(split_lines(v.body)),
                "contributors": contributors}

    def verify(self, doc_id, number):
        v = self._require_version(doc_id, number)
        return sha256_text(v.body) == v.body_sha256

    # ---------- 内部辅助 ----------

    def _build(self, doc_id, actor, body, parent, event_iso, recorded):
        number = parent.version + 1 if parent is not None else 1
        old = split_lines(parent.body) if parent is not None else []
        new = split_lines(body)
        parent_blame = (self.repo.get_blame(doc_id, parent.version)
                        if parent is not None else [])
        diffs, blame = compute_diff_and_blame(
            old, new, parent_blame, number, actor, event_iso)
        version = VersionRecord(
            doc_id=doc_id, version=number, actor=actor,
            parent=parent.version if parent is not None else None,
            body=body, body_sha256=sha256_text(body),
            event_time=event_iso, recorded_time=recorded.isoformat())
        return version, diffs, blame

    def _idem_hit(self, key):
        if not key:
            return None
        return self.repo.get_idem(key)

    def _require_doc(self, doc_id):
        if not self.repo.document_exists(doc_id):
            raise NotFound(f"document {doc_id!r} not found")

    def _require_head(self, doc_id):
        self._require_doc(doc_id)
        head = self.repo.head_version_number(doc_id)
        if head is None:
            raise NotFound(f"document {doc_id!r} has no versions")
        return self.repo.get_version(doc_id, head)

    def _require_version(self, doc_id, number):
        self._require_doc(doc_id)
        v = self.repo.get_version(doc_id, number)
        if v is None:
            raise NotFound(f"version {number} not found")
        return v

    @staticmethod
    def _status(number, published, head):
        if published is not None:
            if number == published:
                return "published"
            return "draft" if number > published else "superseded"
        return "draft" if number == head else "superseded"

    @staticmethod
    def _parse_bound(value, name):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _pinned(self, doc_id, version):
        if version is not None:
            return int(version), "version"
        pub = self.repo.get_current_publication(doc_id)
        if pub is None:
            raise NotFound(f"document {doc_id!r} has no publication; "
                           "pin an explicit version")
        return pub.version, "published"
