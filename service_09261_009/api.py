"""JSON HTTP 边界：把请求映射到 Workflow 命令/查询。"""
from dataclasses import asdict
from urllib.parse import parse_qs, urlsplit

from .workflow import Conflict, NotFound


def dispatch(flow, method, path, body=None, query=None):
    body = body or {}
    parts = [p for p in urlsplit(path).path.strip("/").split("/") if p]
    qs = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
    if query:
        qs.update(query)

    try:
        if method == "POST" and parts == ["docs"]:
            return 201, asdict(flow.create_document(
                body["id"], body["actor"], body.get("body", ""),
                body.get("event_time"), body.get("idempotency_key")))

        if method == "POST" and len(parts) == 3 and parts[0] == "docs" \
                and parts[2] == "commit":
            return 200, asdict(flow.commit(
                parts[1], body["actor"], body["body"],
                body.get("event_time"), body.get("idempotency_key")))

        if method == "POST" and len(parts) == 3 and parts[0] == "docs" \
                and parts[2] == "publish":
            return 200, asdict(flow.publish(
                parts[1], body["actor"], body.get("version"),
                body.get("event_time"), body.get("idempotency_key")))

        if method == "GET" and len(parts) == 2 and parts[0] == "docs":
            return 200, flow.versions(parts[1])

        if method == "GET" and len(parts) == 4 and parts[0] == "docs" \
                and parts[2] == "versions":
            return 200, flow.version_view(parts[1], int(parts[3]))

        if method == "GET" and len(parts) == 5 and parts[0] == "docs" \
                and parts[2] == "versions" and parts[4] == "diff":
            return 200, flow.diff_view(parts[1], int(parts[3]))

        if method == "GET" and len(parts) == 3 and parts[0] == "docs" \
                and parts[2] == "published":
            return 200, flow.published_view(parts[1])

        if method == "GET" and len(parts) == 3 and parts[0] == "docs" \
                and parts[2] == "contributions":
            return 200, flow.contributions(
                parts[1], actor=qs.get("actor"),
                introduced_from=qs.get("introduced_from"),
                introduced_to=qs.get("introduced_to"),
                version=qs.get("version"))

        if method == "GET" and len(parts) == 3 and parts[0] == "docs" \
                and parts[2] == "summary":
            return 200, flow.summary(parts[1], version=qs.get("version"))

        return 404, {"error": "not_found"}
    except NotFound as exc:
        return 404, {"error": "not_found", "message": str(exc)}
    except Conflict as exc:
        return 409, {"error": "conflict", "message": str(exc)}
    except (KeyError, TypeError, ValueError) as exc:
        return 400, {"error": "bad_request", "message": str(exc)}
