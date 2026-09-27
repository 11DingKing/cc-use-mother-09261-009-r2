"""版本化业务工作流：修订、发布封存、基于锚点的贡献查询与汇总。

关键约定：
- version_no 只按提交（落库）顺序单调递增，与业务时间 event_time 无关，
  因此跨天补录只会得到更大的版本号，不会改写历史。
- 每条修订都显式记录 parent_no；贡献查询只在锚定版本的祖先链内进行，
  基于过期版本分叉出的稿件即使条件匹配也不会混入。
- 发布独立封存当时内容与校验和；之后任何修订都不影响已发布版本。
"""

from datetime import datetime, timezone

from . import textbook


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class Workflow:
    def __init__(self, store):
        self.store = store

    # ---------- 写入 ----------

    def create_document(self, doc_id, actor, content, event_time=None,
                        idem_key=None):
        return self._idempotent("create", idem_key, lambda: self._do_create(
            doc_id, actor, content, event_time))

    def _do_create(self, doc_id, actor, content, event_time):
        if self.store.latest_version_no(doc_id) is not None:
            raise ValueError("document already exists")
        event_time = event_time or _now_iso()
        record_time = _now_iso()
        content_hash = textbook.content_digest(content)
        ops = []
        diff_hash = textbook.diff_digest(ops)
        checksum = textbook.record_checksum(
            doc_id, 1, None, actor, content_hash, diff_hash,
            event_time, record_time, "",
        )
        rec = {
            "doc_id": doc_id, "version_no": 1, "parent_no": None,
            "actor": actor, "content": content, "content_hash": content_hash,
            "ops": ops, "diff_hash": diff_hash, "checksum": checksum,
            "event_time": event_time, "record_time": record_time,
        }
        self.store.put_version(rec)
        return rec

    def revise(self, doc_id, actor, content, base_version=None,
               event_time=None, idem_key=None):
        return self._idempotent("revise", idem_key, lambda: self._do_revise(
            doc_id, actor, content, base_version, event_time))

    def _do_revise(self, doc_id, actor, content, base_version, event_time):
        latest_no = self.store.latest_version_no(doc_id)
        if latest_no is None:
            raise ValueError("document not found")
        parent_no = latest_no if base_version is None else base_version
        parent = self.store.get_version(doc_id, parent_no)
        if parent is None:
            raise ValueError("base version not found")
        event_time = event_time or _now_iso()
        record_time = _now_iso()
        version_no = latest_no + 1
        content_hash = textbook.content_digest(content)
        ops = textbook.diff_opcodes(parent["content"], content)
        diff_hash = textbook.diff_digest(ops)
        checksum = textbook.record_checksum(
            doc_id, version_no, parent_no, actor, content_hash, diff_hash,
            event_time, record_time, parent["checksum"],
        )
        rec = {
            "doc_id": doc_id, "version_no": version_no,
            "parent_no": parent_no, "actor": actor, "content": content,
            "content_hash": content_hash, "ops": ops, "diff_hash": diff_hash,
            "checksum": checksum, "event_time": event_time,
            "record_time": record_time,
        }
        self.store.put_version(rec)
        return rec

    def publish(self, doc_id, actor, version_no=None, event_time=None,
                idem_key=None):
        return self._idempotent("publish", idem_key, lambda: self._do_publish(
            doc_id, actor, version_no, event_time))

    def _do_publish(self, doc_id, actor, version_no, event_time):
        target_no = version_no
        if target_no is None:
            target_no = self.store.latest_version_no(doc_id)
        version = self.store.get_version(doc_id, target_no)
        if version is None:
            raise ValueError("version not found")
        editions = self.store.list_publications(doc_id)
        edition_no = len(editions) + 1
        prev_pub_checksum = editions[-1]["checksum"] if editions else ""
        event_time = event_time or _now_iso()
        record_time = _now_iso()
        # 封存内容取自版本记录本身，发布后版本内容不可变（只插入），
        # publications 表再独立保存一份内容副本用于对外读取。
        content = version["content"]
        content_hash = textbook.content_digest(content)
        checksum = textbook.publication_checksum(
            doc_id, edition_no, target_no, content_hash, actor,
            event_time, record_time, prev_pub_checksum,
        )
        rec = {
            "doc_id": doc_id, "edition_no": edition_no,
            "version_no": target_no, "actor": actor, "content": content,
            "content_hash": content_hash, "checksum": checksum,
            "event_time": event_time, "record_time": record_time,
        }
        self.store.put_publication(rec)
        return rec

    def _idempotent(self, scope, key, action):
        if key:
            existing = self.store.lookup_idempotency(scope, key)
            if existing is not None:
                return self.store.get_version(
                    existing["doc_id"], existing["version_no"]
                ) if scope != "publish" else self.store.get_publication(
                    existing["doc_id"], existing["version_no"]
                )
        if scope == "create":
            rec = action()
        else:
            rec = action()
        if key:
            self.store.store_idempotency(
                scope, key, rec["doc_id"],
                rec["version_no"] if scope != "publish" else rec["edition_no"],
            )
        return rec

    # ---------- 读取 ----------

    def history(self, doc_id):
        return [self._version_summary(v) for v in self.store.list_versions(doc_id)]

    def get_diff(self, doc_id, version_no):
        v = self.store.get_version(doc_id, version_no)
        if v is None:
            raise ValueError("version not found")
        parent = self.store.get_version(doc_id, v["parent_no"]) if v["parent_no"] else None
        old = parent["content"] if parent else ""
        return {
            "doc_id": doc_id,
            "version_no": version_no,
            "parent_no": v["parent_no"],
            "actor": v["actor"],
            "content_hash": v["content_hash"],
            "diff_hash": v["diff_hash"],
            "checksum": v["checksum"],
            "stats": textbook.diff_stats(v["ops"]),
            "changes": textbook.render_diff(v["ops"], old, v["content"]),
        }

    def published(self, doc_id, edition_no=None):
        p = self.store.get_publication(doc_id, edition_no)
        if p is None:
            raise ValueError("publication not found")
        return p

    def _resolve_anchor(self, doc_id, anchor):
        """解析查询锚点：显式版本 > 最新发布 > 最新版本。"""
        if anchor is not None:
            v = self.store.get_version(doc_id, anchor)
            if v is None:
                raise ValueError("anchor version not found")
            source = "version"
            return anchor, source
        pub = self.store.get_publication(doc_id)
        if pub is not None:
            return pub["version_no"], "publication"
        latest = self.store.latest_version_no(doc_id)
        if latest is None:
            raise ValueError("document not found")
        return latest, "latest"

    def _ancestors(self, doc_id, anchor_no):
        """沿 parent 链收集锚点的全部祖先（含自身），版本号降序。"""
        chain = []
        seen = set()
        no = anchor_no
        while no is not None:
            if no in seen:
                raise ValueError("version cycle detected")
            v = self.store.get_version(doc_id, no)
            if v is None:
                raise ValueError("broken parent chain")
            seen.add(no)
            chain.append(v)
            no = v["parent_no"]
        return chain

    @staticmethod
    def _match(v, actor, since, until):
        if actor is not None and v["actor"] != actor:
            return False
        if since is not None and v["event_time"] < since:
            return False
        if until is not None and v["event_time"] > until:
            return False
        return True

    def contributions(self, doc_id, anchor=None, actor=None,
                      since=None, until=None):
        """返回锚定版本祖先链内、满足条件的贡献记录。

        筛选只在祖先集内收窄：锚点之外的分叉版本永远不会出现，
        即使它们的 actor/event_time 条件匹配。
        """
        anchor_no, anchor_source = self._resolve_anchor(doc_id, anchor)
        chain = self._ancestors(doc_id, anchor_no)
        records = []
        for v in sorted(chain, key=lambda x: x["version_no"]):
            if not self._match(v, actor, since, until):
                continue
            stats = textbook.diff_stats(v["ops"])
            records.append({
                "version_no": v["version_no"],
                "parent_no": v["parent_no"],
                "actor": v["actor"],
                "event_time": v["event_time"],
                "record_time": v["record_time"],
                "content_hash": v["content_hash"],
                "diff_hash": v["diff_hash"],
                "checksum": v["checksum"],
                "stats": stats,
            })
        return {
            "doc_id": doc_id,
            "anchor_version": anchor_no,
            "anchor_source": anchor_source,
            "filters": {"actor": actor, "since": since, "until": until},
            "contributions": records,
        }

    def summary(self, doc_id, anchor=None, since=None, until=None):
        """按作者汇总锚定祖先链内的贡献行数（基于差异统计）。"""
        anchor_no, anchor_source = self._resolve_anchor(doc_id, anchor)
        chain = self._ancestors(doc_id, anchor_no)
        per_actor = {}
        for v in sorted(chain, key=lambda x: x["version_no"]):
            if not self._match(v, None, since, until):
                continue
            stats = textbook.diff_stats(v["ops"])
            entry = per_actor.setdefault(v["actor"], {
                "actor": v["actor"], "revisions": 0,
                "inserted_lines": 0, "deleted_lines": 0,
                "first_event_time": v["event_time"],
                "last_event_time": v["event_time"],
                "versions": [],
            })
            entry["revisions"] += 1
            entry["inserted_lines"] += stats["inserted_lines"]
            entry["deleted_lines"] += stats["deleted_lines"]
            entry["versions"].append(v["version_no"])
            entry["first_event_time"] = min(entry["first_event_time"], v["event_time"])
            entry["last_event_time"] = max(entry["last_event_time"], v["event_time"])
        return {
            "doc_id": doc_id,
            "anchor_version": anchor_no,
            "anchor_source": anchor_source,
            "filters": {"since": since, "until": until},
            "actors": [per_actor[k] for k in sorted(per_actor)],
        }

    def blame(self, doc_id, anchor=None):
        """锚定版本每一行的引入版本。"""
        anchor_no, anchor_source = self._resolve_anchor(doc_id, anchor)
        origins = textbook.origin_lines(
            lambda d, no: self.store.get_version(d, no), doc_id, anchor_no
        )
        anchor_version = self.store.get_version(doc_id, anchor_no)
        rows = []
        for line, origin_no in zip(anchor_version["content"].splitlines(), origins):
            origin = self.store.get_version(doc_id, origin_no)
            rows.append({
                "line": line,
                "introduced_version": origin_no,
                "actor": origin["actor"],
                "checksum": origin["checksum"],
            })
        return {
            "doc_id": doc_id,
            "anchor_version": anchor_no,
            "anchor_source": anchor_source,
            "content_hash": anchor_version["content_hash"],
            "lines": rows,
        }

    def verify(self, doc_id):
        return self.store.verify(doc_id)

    @staticmethod
    def _version_summary(v):
        return {
            "version_no": v["version_no"],
            "parent_no": v["parent_no"],
            "actor": v["actor"],
            "event_time": v["event_time"],
            "record_time": v["record_time"],
            "content_hash": v["content_hash"],
            "diff_hash": v["diff_hash"],
            "checksum": v["checksum"],
            "stats": textbook.diff_stats(v["ops"]),
        }
