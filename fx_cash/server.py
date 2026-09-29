"""HTTP 服务：JSON API + 静态前端（仅标准库）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from db import Database, connect
from services import (
    FX_DECIMALS,
    InsufficientStock,
    PermissionDenied,
    ServiceError,
    FxCashService,
)

STATIC_DIR = Path(__file__).parent / "static"


def _money(x, dp=4):
    return None if x is None else round(float(x), dp)


class App:
    def __init__(self, db_path: str = "fx.db"):
        self.db = Database(db_path)
        self.svc = FxCashService(self.db)

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    # ---------------- 查询 ----------------
    def inventory_matrix(self, branch: str) -> dict:
        currencies = [dict(r) for r in self.conn.execute(
            "SELECT * FROM currencies ORDER BY code")]
        denoms = [dict(r) for r in self.conn.execute(
            "SELECT * FROM denominations ORDER BY currency,seq")]
        branches = [dict(r) for r in self.conn.execute("SELECT * FROM branches ORDER BY code")]
        stock = {}
        for r in self.conn.execute(
            "SELECT denom_id,total_qty,locked_qty FROM inventory WHERE branch_code=?",
            (branch,),
        ):
            stock[r["denom_id"]] = {"total": r["total_qty"], "locked": r["locked_qty"],
                                    "free": r["total_qty"] - r["locked_qty"]}
        return {"branches": branches, "currencies": currencies,
                "denominations": denoms, "stock": stock, "branch": branch}

    def rates(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT rv.*, c.symbol FROM rate_versions rv JOIN currencies c ON c.code=rv.currency "
            "ORDER BY rv.currency, rv.eff_date DESC, rv.id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["rate"] = _money(d["rate"])
            out.append(d)
        return out

    def _reservation_json(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["cny_amount"] = _money(d["cny_amount"], 2)
        d["fx_rate"] = _money(d["fx_rate"])
        lines = []
        for ln in self.svc._active_lines(r["id"]):
            lines.append({"line_id": ln["id"], "denom_id": ln["denom_id"],
                          "face_value": ln["face_value"], "qty": ln["qty"],
                          "kind": ln["kind"],
                          "subtotal": _money(ln["face_value"] * ln["qty"])})
        d["lines"] = lines
        d["branch_name"] = self.conn.execute(
            "SELECT name FROM branches WHERE code=?", (r["branch_code"],)).fetchone()["name"]
        locks = self.conn.execute(
            "SELECT denom_id,qty,state FROM inventory_locks WHERE reservation_id=? AND state='held'",
            (r["id"],)).fetchall()
        d["held"] = [{"denom_id": x["denom_id"], "qty": x["qty"]} for x in locks]
        return d

    def reservations(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM reservations ORDER BY created_at DESC,id DESC").fetchall()
        return [self._reservation_json(r) for r in rows]

    def reservation(self, rid: str) -> dict:
        return self._reservation_json(self.svc._get(rid))

    def deliveries(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT dl.*, d.face_value, d.currency, r.customer_name, r.branch_code, "
            "t1.name AS teller_name, t2.name AS reviewer_name "
            "FROM deliveries dl "
            "JOIN denominations d ON d.id=dl.denom_id "
            "JOIN reservations r ON r.id=dl.reservation_id "
            "JOIN tellers t1 ON t1.id=dl.delivered_by "
            "LEFT JOIN tellers t2 ON t2.id=dl.reviewed_by "
            "ORDER BY dl.id DESC").fetchall()
        return [dict(r) for r in rows]

    def tellers(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM tellers ORDER BY id")]

    def events(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def calendar(self, month: str) -> dict:
        """month=YYYY-MM；返回每日预约（创建日/到期日落格）。"""
        y, m = map(int, month.split("-"))
        first = datetime(y, m, 1)
        next_m = datetime(y + (m // 12), (m % 12) + 1, 1)
        rows = self.conn.execute(
            "SELECT id,customer_name,currency,status,expires_at,created_at,cny_amount,branch_code "
            "FROM reservations WHERE created_at < ? AND expires_at >= ? "
            "ORDER BY expires_at",
            (next_m.isoformat(), first.date().isoformat()),
        ).fetchall()
        days: dict[str, list] = {}
        for r in rows:
            item = dict(r)
            item["cny_amount"] = _money(r["cny_amount"], 2)
            days.setdefault(r["created_at"][:10], []).append({**item, "marker": "created"})
            days.setdefault(r["expires_at"][:10], []).append({**item, "marker": "deadline"})
        return {"month": month,
                "first_weekday": first.weekday(),  # Mon=0
                "days_in_month": (next_m - first).days,
                "days": days}


# ---------------- HTTP Handler ----------------
class Handler(BaseHTTPRequestHandler):
    app: App = None

    def log_message(self, fmt, *args):
        pass  # 静默；业务流水见 events 表

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode())

    def _error(self, e: Exception) -> None:
        if isinstance(e, InsufficientStock):
            self._send(409, {"error": str(e), "code": "stock_short"})
        elif isinstance(e, PermissionDenied):
            self._send(403, {"error": str(e), "code": "permission_denied"})
        elif isinstance(e, ServiceError):
            self._send(422, {"error": str(e), "code": "business_rule"})
        else:
            self._send(500, {"error": str(e), "code": "server_error"})

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/" or p == "/index.html":
                return self._static("index.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if p == "/api/inventory":
                from urllib.parse import parse_qs
                branch = parse_qs(u.query).get("branch", ["B01"])[0]
                return self._send(200, self.app.inventory_matrix(branch))
            if p == "/api/reservations":
                return self._send(200, self.app.reservations())
            if p.startswith("/api/reservations/"):
                return self._send(200, self.app.reservation(p.rsplit("/", 1)[-1]))
            if p == "/api/rates":
                return self._send(200, self.app.rates())
            if p == "/api/deliveries":
                return self._send(200, self.app.deliveries())
            if p == "/api/tellers":
                return self._send(200, self.app.tellers())
            if p == "/api/events":
                return self._send(200, self.app.events())
            if p == "/api/calendar":
                from urllib.parse import parse_qs
                month = parse_qs(u.query).get("month", [datetime.now().strftime("%Y-%m")])[0]
                return self._send(200, self.app.calendar(month))
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._error(e)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            body = self._read_json()
            s = self.app.svc
            if p == "/api/reservations":
                rid = s.create_reservation(
                    body["customer_name"], body["id_doc"], body["currency"],
                    body["branch_code"], body["lines"], body["expires_at"],
                    body.get("teller", "T01"))
                return self._send(201, {"id": rid})
            parts = p.strip("/").split("/")
            # /api/reservations/{id}/...
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "reservations":
                rid = parts[2]
                action = parts[3] if len(parts) > 3 else ""
                if action == "submit":
                    s.submit(rid, body.get("teller", "T01"))
                elif action == "confirm":
                    s.confirm(rid, body.get("teller", "T01"), body.get("date"))
                elif action == "cancel":
                    s.cancel(rid, body.get("reason", "客户取消"), body.get("teller", "T01"))
                elif action == "expire":
                    return self._send(200, {"expired": s.expire_due()})
                elif action == "deliver":
                    res = s.deliver(rid, body.get("teller", "T01"),
                                    body.get("idempotency_key") or
                                    f"K-{rid}-{datetime.now().strftime('%H%M%S%f')}",
                                    body.get("overrides"))
                    return self._send(200, {"idempotent": res.idempotent, "rows": res.rows})
                elif action == "replacements":
                    if len(parts) > 4 and parts[4] == "accept":
                        s.accept_replacement(rid, body.get("teller", "T01"))
                    elif len(parts) > 4 and parts[4] == "decline":
                        s.decline_replacement(rid, body.get("teller", "T01"))
                    else:
                        s.offer_replacement(rid, body["new_lines"], body.get("teller", "T01"))
                else:
                    return self._send(404, {"error": "unknown action"})
                return self._send(200, {"ok": True})
            # /api/deliveries/{id}/review|amend
            if len(parts) >= 4 and parts[1] == "deliveries":
                did = int(parts[2])
                if parts[3] == "review":
                    s.review_delivery(did, body["supervisor"], body.get("note", ""))
                elif parts[3] == "amend":
                    s.amend_delivery(did, body.get("teller", "T01"), int(body["new_counted"]))
                else:
                    return self._send(404, {"error": "unknown action"})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._error(e)

    def _static(self, name: str):
        fp = (STATIC_DIR / name).resolve()
        if not str(fp).startswith(str(STATIC_DIR.resolve())) or not fp.is_file():
            return self._send(404, {"error": "not found"})
        ctype = {"index.html": "text/html; charset=utf-8",
                 "app.js": "application/javascript; charset=utf-8",
                 "styles.css": "text/css; charset=utf-8"}.get(fp.name, "application/octet-stream")
        self._send(200, fp.read_bytes(), ctype)


def serve(db_path: str = "fx.db", host: str = "127.0.0.1", port: int = 8080) -> None:
    Handler.app = App(db_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"外币现钞兑换预约平台已启动: http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
