"""JSON HTTP 边界：把请求映射到 Workflow，ValueError 归为 400。

query 为查询参数字典（由外层 Web 框架解析后传入）。
"""


def dispatch(flow, method, path, body=None, query=None):
    body = body or {}
    query = query or {}
    try:
        return _route(flow, method, path, body, query)
    except ValueError as exc:
        return 400, {"error": str(exc)}


def _route(flow, method, path, body, query):
    parts = [p for p in path.strip("/").split("/") if p != ""]

    if method == "POST" and parts == ["docs"]:
        rec = flow.create_document(
            body["id"], body["actor"], body["content"],
            body.get("event_time"), body.get("idempotency_key"),
        )
        return 201, _version_payload(rec)

    if len(parts) == 3 and parts[0] == "docs" and parts[2] == "revisions" \
            and method == "POST":
        rec = flow.revise(
            parts[1], body["actor"], body["content"],
            body.get("base_version"), body.get("event_time"),
            body.get("idempotency_key"),
        )
        return 201, _version_payload(rec)

    if len(parts) == 3 and parts[0] == "docs" and parts[2] == "publications" \
            and method == "POST":
        rec = flow.publish(
            parts[1], body["actor"], body.get("version_no"),
            body.get("event_time"), body.get("idempotency_key"),
        )
        return 201, rec

    if len(parts) == 2 and parts[0] == "docs" and method == "GET":
        return 200, {"doc_id": parts[1], "history": flow.history(parts[1])}

    if len(parts) == 4 and parts[0] == "docs" and parts[2] == "versions" \
            and parts[3] == "diff" and method == "GET":
        return 200, flow.get_diff(parts[1], int(query.get("v")))

    if len(parts) == 3 and parts[0] == "docs" and parts[2] == "published" \
            and method == "GET":
        edition = query.get("edition")
        return 200, flow.published(parts[1], int(edition) if edition else None)

    if len(parts) == 3 and parts[0] == "docs" and parts[2] == "contributions" \
            and method == "GET":
        return 200, flow.contributions(
            parts[1], _int(query.get("anchor")), query.get("actor"),
            query.get("since"), query.get("until"),
        )

    if len(parts) == 3 and parts[0] == "docs" and parts[2] == "summary" \
            and method == "GET":
        return 200, flow.summary(
            parts[1], _int(query.get("anchor")),
            query.get("since"), query.get("until"),
        )

    if len(parts) == 3 and parts[0] == "docs" and parts[2] == "blame" \
            and method == "GET":
        return 200, flow.blame(parts[1], _int(query.get("anchor")))

    if len(parts) == 3 and parts[0] == "docs" and parts[2] == "verify" \
            and method == "GET":
        return 200, flow.verify(parts[1])

    return 404, {"error": "not_found"}


def _int(value):
    return int(value) if value is not None else None


def _version_payload(rec):
    # 写入返回不含全文的摘要 + 校验信息，全文走历史/差异接口
    return {
        "doc_id": rec["doc_id"],
        "version_no": rec["version_no"],
        "parent_no": rec["parent_no"],
        "actor": rec["actor"],
        "content_hash": rec["content_hash"],
        "diff_hash": rec["diff_hash"],
        "checksum": rec["checksum"],
        "event_time": rec["event_time"],
        "record_time": rec["record_time"],
    }
