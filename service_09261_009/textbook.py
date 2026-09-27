"""教材版本的纯领域逻辑：差异、摘要、哈希链。

本模块不接触数据库，所有函数都是确定性的，便于单测与跨天补录场景复用。
"""

import difflib
import hashlib
import json

RECORD_PREFIX = "textbook-version-v1"
PUBLICATION_PREFIX = "textbook-publication-v1"


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(payload):
    """校验用的规范化 JSON：排序键、紧凑分隔，保证跨进程可复算。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def split_lines(content):
    return content.splitlines()


def content_digest(content):
    return sha256_hex(content)


def diff_opcodes(old_content, new_content):
    """返回 difflib opcodes 的可 JSON 化列表：[tag, i1, i2, j1, j2]。

    左（i）索引对应父版本行，右（j）索引对应当前版本行。
    """
    old_lines = split_lines(old_content)
    new_lines = split_lines(new_content)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    return [list(op) for op in matcher.get_opcodes()]


def diff_digest(ops):
    return sha256_hex(canonical(ops))


def record_checksum(doc_id, version_no, parent_no, actor,
                    content_hash, diff_hash, event_time, record_time,
                    prev_checksum):
    """版本记录校验和：把归属、父子关系与父版校验和全部绑定进来。"""
    payload = [
        RECORD_PREFIX, doc_id, version_no, parent_no, actor,
        content_hash, diff_hash, event_time, record_time, prev_checksum,
    ]
    return sha256_hex(canonical(payload))


def publication_checksum(doc_id, edition_no, version_no, content_hash,
                         actor, event_time, record_time, prev_checksum):
    payload = [
        PUBLICATION_PREFIX, doc_id, edition_no, version_no,
        content_hash, actor, event_time, record_time, prev_checksum,
    ]
    return sha256_hex(canonical(payload))


def diff_stats(ops):
    """相对父版本的增删行统计。"""
    inserted = deleted = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag in ("insert", "replace"):
            inserted += j2 - j1
        if tag in ("delete", "replace"):
            deleted += i2 - i1
    return {"inserted_lines": inserted, "deleted_lines": deleted}


def render_diff(ops, old_content, new_content):
    """把 opcodes 展开成带文本的差异，供审计接口直接阅读。"""
    old_lines = split_lines(old_content)
    new_lines = split_lines(new_content)
    rendered = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        rendered.append({
            "op": tag,
            "old_lines": old_lines[i1:i2],
            "new_lines": new_lines[j1:j2],
        })
    return rendered


def origin_lines(get_version, doc_id, anchor_no):
    """追溯锚定版本每一行最初由哪个版本引入（blame）。

    get_version(doc_id, version_no) 返回含 parent_no/content/ops 的版本记录。
    返回与锚定版本行对齐的版本号列表；只沿 parent 链回溯，分叉版本天然不可达。
    """
    cache = {}

    def resolve(no):
        if no in cache:
            return cache[no]
        version = get_version(doc_id, no)
        lines = split_lines(version["content"])
        parent_no = version["parent_no"]
        if parent_no is None:
            origins = [no] * len(lines)
        else:
            parent_origins = resolve(parent_no)
            origins = [None] * len(lines)
            for tag, i1, i2, j1, j2 in version["ops"]:
                if tag == "equal":
                    origins[j1:j2] = parent_origins[i1:i2]
                else:
                    # insert/replace 产生的新行归当前版本；delete 在右侧不占行
                    for k in range(j1, j2):
                        origins[k] = no
        cache[no] = origins
        return origins

    return resolve(anchor_no)
