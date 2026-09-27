import os
import tempfile
import unittest

from service_09261_009 import SQLiteStore, Workflow
from service_09261_009.api import dispatch
from service_09261_009.workflow import sha256_text


def make_flow(store=None):
    return Workflow(repo=store)


class TestDiffAndAttribution(unittest.TestCase):
    def test_each_version_stores_diff_against_parent(self):
        f = make_flow()
        f.create_document("d1", "alice", "第一章\n引言\n", idempotency_key="c")
        v2 = f.commit("d1", "bob", "第一章\n引言\n新增一节\n")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.parent, 1)
        diff = f.diff_view("d1", 2)
        tags = [(r["tag"], r["old_lines"], r["new_lines"])
                for r in diff["records"]]
        self.assertIn(("insert", [], ["新增一节"]),
                      [(t[0], t[1], t[2]) for t in tags])
        # 无变化行不出现在 insert/replace 中
        changed = [r for r in diff["records"] if r["tag"] != "equal"]
        self.assertTrue(all(r["actor"] == "bob" for r in changed))

    def test_full_overwrite_keeps_original_attribution(self):
        """编辑直接覆盖旧稿：未改动的原行仍归原作者。"""
        f = make_flow()
        f.create_document("d1", "alice", "保留行\n旧行\n")
        f.commit("d1", "bob", "保留行\n新行\n")
        blame = f.repo.get_blame("d1", 2)
        actors = {b.ordinal: (b.actor, b.introduced_version) for b in blame}
        self.assertEqual(actors[1], ("alice", 1))
        self.assertEqual(actors[2], ("bob", 2))
        f.publish("d1", "editor")
        contrib = f.contributions("d1")
        by_line = {r["ordinal"]: r["actor"] for r in contrib["records"]}
        self.assertEqual(by_line, {1: "alice", 2: "bob"})


class TestPublishPinning(unittest.TestCase):
    def _flow(self):
        f = make_flow()
        f.create_document("d1", "alice", "v1内容\n")
        f.commit("d1", "bob", "v2内容\n")
        f.publish("d1", "editor")
        return f

    def test_publish_freezes_body_and_checksum(self):
        f = self._flow()
        pub = f.published_view("d1")
        self.assertEqual(pub["version"], 2)
        self.assertEqual(pub["body"], "v2内容\n")
        self.assertEqual(pub["body_sha256"], sha256_text("v2内容\n"))
        self.assertTrue(pub["verified"])

    def test_stale_draft_does_not_mix_into_queries_or_summary(self):
        f = self._flow()
        # 发布后又提交新稿（未发布），再补一个被废弃的旧版本状态
        f.commit("d1", "carol", "v3草稿内容\n")
        contrib = f.contributions("d1")
        self.assertEqual(contrib["scope"], "published")
        self.assertEqual(contrib["version"], 2)
        self.assertNotIn("v3草稿内容", [r["line"] for r in contrib["records"]])
        summary = f.summary("d1")
        self.assertEqual(summary["version"], 2)
        self.assertEqual({c["actor"] for c in summary["contributors"]},
                         {"bob"})
        # 版本状态标记正确：1 已过期、2 已发布、3 是新草稿
        statuses = {v["version"]: v["status"] for v in f.versions("d1")}
        self.assertEqual(statuses, {1: "superseded", 2: "published",
                                    3: "draft"})

    def test_republish_pins_new_content(self):
        f = self._flow()
        f.commit("d1", "carol", "v3内容\n")
        f.publish("d1", "editor")
        pub = f.published_view("d1")
        self.assertEqual(pub["version"], 3)
        summary = f.summary("d1")
        self.assertEqual({c["actor"] for c in summary["contributors"]},
                         {"carol"})
        # 显式查询旧版本仍可追溯其归属
        old = f.contributions("d1", version=2)
        self.assertEqual(old["scope"], "version")
        self.assertEqual({r["actor"] for r in old["records"]}, {"bob"})


class TestBackfillAndFilters(unittest.TestCase):
    def test_cross_day_backfill_keeps_version_and_checksum_aligned(self):
        f = make_flow()
        # 跨天补录：声称的贡献时间早于实际入库时间
        f.create_document("d1", "alice", "第一天写的\n",
                          event_time="2026-09-20T10:00:00+00:00")
        f.commit("d1", "bob", "第一天写的\n第二天补写的\n",
                 event_time="2026-09-21T10:00:00+00:00")
        f.publish("d1", "editor",
                  event_time="2026-09-21T12:00:00+00:00")
        contrib = f.contributions("d1")
        self.assertTrue(all(r["line_verified"] for r in contrib["records"]))
        by_actor = {}
        for r in contrib["records"]:
            by_actor.setdefault(r["actor"], []).append(r)
        self.assertEqual(by_actor["alice"][0]["introduced_version"], 1)
        self.assertEqual(by_actor["bob"][0]["introduced_version"], 2)
        self.assertTrue(contrib["verified"])

        # 按时间窗口筛选：只看第一天的贡献
        day1 = f.contributions(
            "d1", introduced_from="2026-09-20T00:00:00+00:00",
            introduced_to="2026-09-20T23:59:59+00:00")
        self.assertEqual([r["line"] for r in day1["records"]],
                         ["第一天写的"])
        self.assertEqual(day1["records"][0]["introduced_version"], 1)

        # 按作者筛选，记录仍与发布版本对齐
        bob = f.contributions("d1", actor="bob")
        self.assertEqual(bob["version"], 2)
        self.assertEqual(len(bob["records"]), 1)
        self.assertEqual(
            bob["records"][0]["line_sha256"],
            sha256_text("第二天补写的"))

    def test_filter_on_pinned_version_does_not_pull_newer_lines(self):
        f = make_flow()
        f.create_document("d1", "alice", "a行\n")
        f.publish("d1", "editor")
        f.commit("d1", "bob", "a行\nb行\n")
        only_a = f.contributions("d1", actor="alice")
        self.assertEqual(only_a["scope"], "published")
        self.assertEqual([r["line"] for r in only_a["records"]], ["a行"])
        explicit = f.contributions("d1", version=2)
        self.assertEqual(len(explicit["records"]), 2)


class TestSQLitePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def test_state_survives_reopen_and_published_snapshot_is_fixed(self):
        store = SQLiteStore(self.path)
        f = Workflow(repo=store)
        f.create_document("d1", "alice", "持久化行1\n")
        f.commit("d1", "bob", "持久化行1\n持久化行2\n")
        f.publish("d1", "editor")
        f.commit("d1", "carol", "未发布草稿\n")
        store.db.close()

        store2 = SQLiteStore(self.path)
        f2 = Workflow(repo=store2)
        pub = f2.published_view("d1")
        self.assertEqual(pub["version"], 2)
        self.assertTrue(pub["verified"])
        summary = f2.summary("d1")
        self.assertEqual({c["actor"]: c["lines"]
                          for c in summary["contributors"]},
                         {"alice": 1, "bob": 1})
        diff = f2.diff_view("d1", 2)
        self.assertTrue(any(r["tag"] == "insert" for r in diff["records"]))
        store2.db.close()


class TestIdempotencyAndAPI(unittest.TestCase):
    def test_idempotent_commit_replays_same_version(self):
        f = make_flow()
        f.create_document("d1", "alice", "x\n", idempotency_key="k1")
        again = f.create_document("d1", "alice", "x\n", idempotency_key="k1")
        self.assertEqual(again.version, 1)
        v = f.commit("d1", "bob", "y\n", idempotency_key="k2")
        v_again = f.commit("d1", "bob", "完全不同的内容\n",
                           idempotency_key="k2")
        self.assertEqual(v_again.version, v.version)
        self.assertEqual(v_again.body, "y\n")
        pub = f.publish("d1", "editor", idempotency_key="k3")
        pub_again = f.publish("d1", "editor", version=1,
                              idempotency_key="k3")
        self.assertEqual(pub_again.version, pub.version)

    def test_api_routes(self):
        f = make_flow()
        code, v1 = dispatch(f, "POST", "/docs",
                            {"id": "d1", "actor": "alice",
                             "body": "api行\n"})
        self.assertEqual(code, 201)
        code, _ = dispatch(f, "POST", "/docs/d1/commit",
                           {"actor": "bob", "body": "api行\n新增\n"})
        self.assertEqual(code, 200)
        code, pub = dispatch(f, "POST", "/docs/d1/publish",
                             {"actor": "editor"})
        self.assertEqual(code, 200)
        self.assertEqual(pub["version"], 2)
        code, summary = dispatch(f, "GET", "/docs/d1/summary")
        self.assertEqual(code, 200)
        self.assertEqual({c["actor"] for c in summary["contributors"]},
                         {"alice", "bob"})
        code, contrib = dispatch(
            f, "GET", "/docs/d1/contributions?actor=bob")
        self.assertEqual(code, 200)
        self.assertTrue(all(r["actor"] == "bob" for r in contrib["records"]))
        code, missing = dispatch(f, "GET", "/docs/nope/summary")
        self.assertEqual(code, 404)
        code, bad = dispatch(f, "POST", "/docs/d1/commit", {"actor": "x"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
