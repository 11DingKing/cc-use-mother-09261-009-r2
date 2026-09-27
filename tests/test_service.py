import unittest

from service_09261_009.store import SQLiteStore
from service_09261_009.workflow import Workflow
from service_09261_009 import textbook
from service_09261_009.api import dispatch


def make_flow():
    return Workflow(SQLiteStore(":memory:"))


class TestRevisionDiff(unittest.TestCase):
    def test_each_revision_stores_diff_and_attribution(self):
        f = make_flow()
        v1 = f.create_document("d1", "alice", "第一章 初稿\n第二行\n")
        v2 = f.revise("d1", "bob", "第一章 修改\n第二行\n新增第三行\n")
        self.assertEqual(v2["parent_no"], 1)
        diff = f.get_diff("d1", 2)
        self.assertEqual(diff["actor"], "bob")
        self.assertEqual(diff["stats"]["inserted_lines"], 2)
        self.assertEqual(diff["stats"]["deleted_lines"], 1)
        ops_flat = [op for change in diff["changes"] for op in [change["op"]]]
        self.assertIn("replace", ops_flat)
        self.assertIn("insert", ops_flat)
        # 校验和与哈希可独立复算
        self.assertEqual(textbook.content_digest(v2["content"]), v2["content_hash"])
        self.assertEqual(textbook.diff_digest(v2["ops"]), v2["diff_hash"])

    def test_no_change_diff_is_empty(self):
        f = make_flow()
        f.create_document("d1", "alice", "相同内容\n")
        v = f.revise("d1", "alice", "相同内容\n")
        self.assertEqual(v["ops"], [["equal", 0, 1, 0, 1]])
        self.assertEqual(f.get_diff("d1", 2)["changes"], [])


class TestPublishFreeze(unittest.TestCase):
    def test_publish_freezes_content_independent_of_later_edits(self):
        f = make_flow()
        f.create_document("d1", "alice", "v1内容\n")
        f.revise("d1", "bob", "v2内容\n")
        pub = f.publish("d1", "editor")
        self.assertEqual(pub["edition_no"], 1)
        self.assertEqual(pub["version_no"], 2)

        # 发布后再改，不影响已发布内容
        f.revise("d1", "carol", "v3内容 不应出现在发布版\n")
        published = f.published("d1")
        self.assertEqual(published["content"], "v2内容\n")
        self.assertEqual(published["content_hash"], pub["content_hash"])

        # 默认贡献查询锚定最新发布版，v3 不混入
        contrib = f.contributions("d1")
        self.assertEqual(contrib["anchor_source"], "publication")
        self.assertEqual(contrib["anchor_version"], 2)
        versions = [c["version_no"] for c in contrib["contributions"]]
        self.assertEqual(versions, [1, 2])

        # 显式锚定 v3 才能看到
        contrib3 = f.contributions("d1", anchor=3)
        self.assertEqual([c["version_no"] for c in contrib3["contributions"]], [1, 2, 3])

        # 汇总同样不混入 v3（carol 不出现在发布版汇总）
        s = f.summary("d1")
        actors = {a["actor"] for a in s["actors"]}
        self.assertEqual(actors, {"alice", "bob"})

        # 第二次发布封存 v3，之后默认锚点随之移动
        f.publish("d1", "editor")
        self.assertEqual(f.published("d1")["version_no"], 3)
        self.assertEqual(f.contributions("d1")["anchor_version"], 3)
        # 指定版次仍能取到第一次发布的固定内容
        self.assertEqual(f.published("d1", edition_no=1)["content"], "v2内容\n")


class TestStaleForkExclusion(unittest.TestCase):
    def test_fork_from_stale_version_never_mixes_into_anchored_query(self):
        f = make_flow()
        f.create_document("d1", "alice", "行1\n行2\n")                       # v1
        f.revise("d1", "bob", "行1改\n行2\n行3\n", event_time="2026-09-20T10:00:00+00:00")  # v2
        f.revise("d1", "carol", "行1改\n行2改\n行3\n行4\n",
                 event_time="2026-09-21T10:00:00+00:00")                    # v3 主干
        f.publish("d1", "editor", version_no=3, event_time="2026-09-21T12:00:00+00:00")

        # 有人次日仍基于已过期的 v2 补录（分叉），业务时间甚至早于 v3
        v4 = f.revise("d1", "dave", "行1改\n行2\n行3\nDAVE分叉行\n",
                      base_version=2, event_time="2026-09-19T08:00:00+00:00")
        self.assertEqual(v4["parent_no"], 2)
        self.assertEqual(v4["version_no"], 4)  # 版本号按提交序，不受业务时间影响

        # 默认锚定发布版 v3：任何条件筛选都查不到 dave 的分叉
        self.assertEqual(
            [c["version_no"] for c in f.contributions("d1", actor="dave")["contributions"]], []
        )
        wide = f.contributions("d1", since="2026-09-01T00:00:00+00:00")
        self.assertEqual(
            {c["version_no"] for c in wide["contributions"]}, {1, 2, 3}
        )
        # 按业务时间窗口筛选，v4 的 event_time 落在窗口内也不混入
        window = f.contributions("d1", since="2026-09-19T00:00:00+00:00",
                                 until="2026-09-19T23:59:59+00:00")
        self.assertEqual(window["contributions"], [])

        # 汇总同理
        s = f.summary("d1")
        self.assertEqual({a["actor"] for a in s["actors"]},
                         {"alice", "bob", "carol"})

        # 显式锚定分叉 v4 时，只看到它自己的祖先链 {1,2,4}，v3 不在
        anchored = f.contributions("d1", anchor=4)
        self.assertEqual(
            [c["version_no"] for c in anchored["contributions"]], [1, 2, 4]
        )
        s4 = f.summary("d1", anchor=4)
        self.assertEqual({a["actor"] for a in s4["actors"]},
                         {"alice", "bob", "dave"})


class TestBlame(unittest.TestCase):
    def test_blame_attributes_each_line_to_introducing_version(self):
        f = make_flow()
        f.create_document("d1", "alice", "a行\nb行\n")
        f.revise("d1", "bob", "a行\nb改\nc行\n")
        blame = f.blame("d1")
        by_line = {row["line"]: row for row in blame["lines"]}
        self.assertEqual(by_line["a行"]["introduced_version"], 1)
        self.assertEqual(by_line["a行"]["actor"], "alice")
        self.assertEqual(by_line["b改"]["introduced_version"], 2)
        self.assertEqual(by_line["b改"]["actor"], "bob")
        self.assertEqual(by_line["c行"]["introduced_version"], 2)


class TestChecksums(unittest.TestCase):
    def test_verify_passes_on_clean_history(self):
        f = make_flow()
        f.create_document("d1", "alice", "内容\n")
        f.revise("d1", "bob", "内容2\n")
        f.publish("d1", "editor")
        result = f.verify("d1")
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["versions_checked"], 2)
        self.assertEqual(result["publications_checked"], 1)

    def test_verify_detects_content_tampering(self):
        f = make_flow()
        f.create_document("d1", "alice", "原始内容\n")
        f.revise("d1", "bob", "新内容\n")
        # 直接在库里改写历史行（模拟存储被篡改）
        f.store.db.execute(
            "UPDATE versions SET content=? WHERE doc_id=? AND version_no=?",
            ("被篡改\n", "d1", 1),
        )
        f.store.db.commit()
        result = f.verify("d1")
        self.assertFalse(result["ok"])
        self.assertTrue(any("v1" in e for e in result["errors"]))

    def test_verify_detects_attribution_tampering(self):
        f = make_flow()
        f.create_document("d1", "alice", "内容\n")
        f.revise("d1", "bob", "内容2\n")
        f.store.db.execute(
            "UPDATE versions SET actor=? WHERE doc_id=? AND version_no=?",
            ("mallory", "d1", 2),
        )
        f.store.db.commit()
        result = f.verify("d1")
        self.assertFalse(result["ok"])
        self.assertTrue(any("checksum chain mismatch" in e for e in result["errors"]))

    def test_verify_detects_publication_freeze_violation(self):
        f = make_flow()
        f.create_document("d1", "alice", "发布内容\n")
        f.publish("d1", "editor")
        f.store.db.execute(
            "UPDATE publications SET content=? WHERE doc_id=? AND edition_no=1",
            ("发布后被改\n", "d1"),
        )
        f.store.db.commit()
        result = f.verify("d1")
        self.assertFalse(result["ok"])


class TestIdempotency(unittest.TestCase):
    def test_same_key_returns_same_record(self):
        f = make_flow()
        r1 = f.create_document("d1", "alice", "x\n", idem_key="k1")
        r2 = f.create_document("d1", "alice", "完全不同的参数\n", idem_key="k1")
        self.assertEqual(r1["version_no"], r2["version_no"])
        self.assertEqual(r2["content_hash"], r1["content_hash"])

        v1 = f.revise("d1", "bob", "y\n", idem_key="k2")
        v2 = f.revise("d1", "bob", "zzz\n", idem_key="k2")
        self.assertEqual(v1["version_no"], v2["version_no"])
        self.assertEqual(f.store.latest_version_no("d1"), 2)

    def test_publish_idempotency(self):
        f = make_flow()
        f.create_document("d1", "alice", "x\n")
        p1 = f.publish("d1", "editor", idem_key="pk")
        f.revise("d1", "bob", "y\n")
        p2 = f.publish("d1", "editor", idem_key="pk")
        self.assertEqual(p1["edition_no"], p2["edition_no"])
        self.assertEqual(len(f.store.list_publications("d1")), 1)


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.flow = make_flow()

    def test_full_flow_over_http(self):
        code, _ = dispatch(self.flow, "POST", "/docs",
                           {"id": "d1", "actor": "alice", "content": "一\n二\n"})
        self.assertEqual(code, 201)
        code, _ = dispatch(self.flow, "POST", "/docs/d1/revisions",
                           {"actor": "bob", "content": "一\n二改\n三\n"})
        self.assertEqual(code, 201)
        code, pub = dispatch(self.flow, "POST", "/docs/d1/publications",
                             {"actor": "editor"})
        self.assertEqual(code, 201)
        self.assertEqual(pub["version_no"], 2)

        code, diff = dispatch(self.flow, "GET", "/docs/d1/versions/diff",
                              query={"v": "2"})
        self.assertEqual(code, 200)
        self.assertEqual(diff["stats"]["inserted_lines"], 2)

        code, contrib = dispatch(self.flow, "GET", "/docs/d1/contributions",
                                 query={"actor": "bob"})
        self.assertEqual(code, 200)
        self.assertEqual(len(contrib["contributions"]), 1)
        self.assertEqual(contrib["contributions"][0]["checksum"],
                         self.flow.get_diff("d1", 2)["checksum"])

        code, verify = dispatch(self.flow, "GET", "/docs/d1/verify")
        self.assertEqual(code, 200)
        self.assertTrue(verify["ok"])

    def test_error_mapping(self):
        code, body = dispatch(self.flow, "POST", "/docs/d1/revisions",
                              {"actor": "bob", "content": "x\n"})
        self.assertEqual(code, 400)
        self.assertIn("error", body)
        code, body = dispatch(self.flow, "GET", "/nope")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
