"""零依赖 HTTP 服务：JSON API + 静态页面，每请求独立 SQLite 连接。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config
from .db import connect, init_db
from .services import appointments as A
from .services import delivery as DV
from .services import fx as FX
from .services import inventory as INV
from .services.errors import PlatformError
from .services.moneyutil import display_money

STATIC_DIR = Path(__file__).resolve().parent / "static"

HTTP_STATUS = {
    "inventory_shortage": 409,
    "concurrent_lock": 409,
    "invalid_state": 409,
    "permission_denied": 403,
    "rate_unavailable": 422,
    "reconfirm_required": 409,
    "business_error": 400,
}


class Api:
    """服务编排：把 HTTP 请求映射到领域服务。"""

    def __init__(self, db_path=None) -> None:
        self.db_path = db_path
        # 到期扫描线程用；HTTP 请求使用各自短连接
        self._scan_lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _actor(body: dict, headers) -> str:
        return (headers.get("x-teller") or body.get("actor") or "").strip()

    @staticmethod
    def _teller_role(conn, username: str) -> str:
        row = conn.execute("SELECT role FROM tellers WHERE username=?",
                           (username,)).fetchone()
        return row["role"] if row else ""

    @staticmethod
    def _enrich_money(payload) -> dict:
        """给预约类结果补固定小数位的展示字段。"""
        ap = payload.get("appointment")
        if ap and ap.get("amount_foreign"):
            ap["amount_foreign_display"] = display_money(ap["amount_foreign"])
            ap["amount_cny_display"] = display_money(ap["amount_cny"])
        return payload

    # ------------------------------------------------------------ routing
    def handle(self, method: str, path: str, qs: dict, body: dict,
               headers) -> tuple[int, dict]:
        conn = connect(self.db_path)
        try:
            return 200, self._route(conn, method, path, qs, body, headers)
        except PlatformError as e:
            return HTTP_STATUS.get(e.code, 400), {
                "error": True, "code": e.code, "message": str(e),
                "shortages": getattr(e, "shortages", None),
            }
        finally:
            conn.close()

    def _route(self, conn, method, path, qs, body, headers):
        p = path.strip("/").split("/") if path != "/" else []
        # ---- 元数据 / 库存 / 日历
        if method == "GET" and path == "/api/meta":
            return self._meta(conn)
        if method == "GET" and path == "/api/inventory":
            return INV.matrix(conn)
        if method == "GET" and path == "/api/appointments":
            return {"appointments": A.list_appointments(
                conn, on_date=qs.get("date", [None])[0])}
        if method == "GET" and path == "/api/calendar":
            return A.calendar(conn)
        if method == "POST" and path == "/api/expire-scan":
            with self._scan_lock:
                return {"expired": A.expire_due(conn, self._actor(body, headers))}

        # ---- 汇率
        if method == "GET" and path == "/api/fx":
            return {"rates": FX.list_rates(conn)}
        if method == "POST" and path == "/api/fx/publish":
            actor = self._actor(body, headers)
            return FX.publish_rate(
                conn, actor, self._teller_role(conn, actor),
                body["currency"], body.get("rate"), body.get("rate_date"))

        code = p[2] if len(p) >= 3 and p[0] == "api" and p[1] == "appointments" else None
        # ---- 单条预约
        if code and method == "GET" and len(p) == 3:
            return self._enrich_money(A.get_detail(conn, code))
        if method == "POST" and path == "/api/appointments":
            return self._create(conn, body, headers)
        if code and method == "POST" and len(p) == 4 and p[3] == "confirm":
            return self._enrich_money(
                A.confirm(conn, code, self._actor(body, headers)))
        if code and method == "POST" and len(p) == 4 and p[3] == "cancel":
            kind = "teller" if body.get("by") == "teller" else "customer"
            return A.cancel(conn, code, self._actor(body, headers),
                            body.get("reason", "客户取消"), kind)
        if code and method == "POST" and "/".join(p[3:]) == "replacement":
            items = {int(k): int(v) for k, v in body["items"].items()}
            return A.propose_replacement(
                conn, code, self._actor(body, headers),
                items, body.get("reason", "券别替换"))
        if code and method == "POST" and "/".join(p[3:]) == "replacement/decision":
            return self._enrich_money(A.decide_replacement(
                conn, code, bool(body.get("accept")),
                self._actor(body, headers)))

        # ---- 柜面核验 / 交付 / 复核
        if method == "POST" and path == "/api/verify":
            return DV.counter_verify(
                conn, body["code"], self._actor(body, headers),
                body.get("counter_branch", "BR001"))
        if method == "POST" and path == "/api/deliver":
            return self._deliver(conn, body, headers)
        if method == "GET" and path == "/api/diffs":
            return {"diffs": DV.list_pending(conn)}
        if method == "POST" and path == "/api/review":
            reviewed = body.get("reviewed")
            reviewed = {int(k): int(v) for k, v in reviewed.items()} if reviewed else None
            return DV.review_delivery(
                conn, body["code"], self._actor(body, headers),
                reviewed, body.get("resolution", ""))
        if method == "POST" and path == "/api/counterfeit/resolve":
            return DV.resolve_counterfeit(
                conn, body["code"], self._actor(body, headers),
                body.get("resolution", "假钞按规程处置"),
                bool(body.get("release_stock", True)))

        raise PlatformError(f"未找到接口: {method} {path}")

    # ------------------------------------------------------------ payloads
    def _meta(self, conn) -> dict:
        currencies = []
        for r in conn.execute(
            "SELECT code,name,country FROM currencies WHERE active=1 ORDER BY code"
        ):
            denoms = [dict(d) for d in conn.execute(
                "SELECT id,face_value,seq FROM denominations"
                " WHERE currency_code=? AND active=1 ORDER BY seq", (r["code"],))]
            currencies.append({**dict(r), "denominations": denoms})
        return {
            "tellers": [dict(r) for r in conn.execute(
                "SELECT username,name,role FROM tellers ORDER BY role,username")],
            "branches": [dict(r) for r in conn.execute(
                "SELECT code,name,address FROM branches ORDER BY code")],
            "currencies": currencies,
        }

    def _create(self, conn, body, headers) -> dict:
        items = {int(k): int(v) for k, v in body["items"].items()}
        det = A.create_reservation(
            conn,
            self._actor(body, headers),
            body["customer"],
            body["currency"],
            body["branch_code"],
            items,
            int(body.get("valid_hours", config.DEFAULT_VALID_HOURS)),
            body.get("note"),
        )
        return self._enrich_money(det)

    def _deliver(self, conn, body, headers) -> dict:
        counted = body.get("counted")
        counted = {int(k): int(v) for k, v in counted.items()} if counted else None
        cf = body.get("counterfeit")
        cf = {int(k): list(v) for k, v in cf.items()} if cf else None
        bundle = DV.deliver(
            conn, body["code"], self._actor(body, headers),
            counted=counted, counterfeit=cf, note=body.get("note"))
        return self._enrich_money(bundle)


class Handler(BaseHTTPRequestHandler):
    server_version = "FXPlatform/1.0"

    def _send_json(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, rel: str) -> None:
        f = (STATIC_DIR / rel).resolve()
        if not str(f).startswith(str(STATIC_DIR)) or not f.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(f.suffix, "application/octet-stream")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._dispatch("GET", parsed)
        elif parsed.path in ("/", "/index.html"):
            self._send_file("index.html")
        elif parsed.path.startswith("/static/"):
            self._send_file(parsed.path[len("/static/"):])
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        self._dispatch("POST", parsed)

    def _dispatch(self, method: str, parsed) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": True, "code": "bad_json",
                                  "message": "请求体不是合法 JSON"})
            return
        try:
            status, payload = self.server.api.handle(
                method, parsed.path, parse_qs(parsed.query), body, self.headers)
            self._send_json(status, payload)
        except Exception as e:  # noqa: BLE001 — 兜底防栈泄漏
            self._send_json(500, {"error": True, "code": "server_error",
                                  "message": str(e)})

    def log_message(self, fmt, *args) -> None:  # 安静日志
        pass


def create_server(host: str | None = None, port: int | None = None,
                  db_path=None) -> ThreadingHTTPServer:
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    server = ThreadingHTTPServer((host or config.HOST, port or config.PORT), Handler)
    server.api = Api(db_path)
    return server


def main() -> None:
    srv = create_server()
    print(f"外币现钞预约与券别核对平台 → http://{srv.server_address[0]}:"
          f"{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
