"""HTTP API 端到端测试：真实端口 + JSON 请求。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from fxplatform.server import Api, Handler


class ApiClient:
    def __init__(self, base: str):
        self.base = base

    def call(self, path: str, body=None, actor: str | None = None,
             method: str | None = None):
        data = json.dumps(body if body is not None else {}).encode()
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Teller"] = actor
        req = urllib.request.Request(
            self.base + path,
            data=data if (body is not None or method == "POST") else None,
            method=method or ("POST" if body is not None else "GET"),
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class HttpTestBase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from fxplatform.db import connect, init_db
        conn = connect(self.db_path)
        init_db(conn)
        # 券别 id 查询
        self.denoms = {}
        for ccy in ("USD", "GBP"):
            self.denoms[ccy] = {
                str(r["face_value"]): r["id"] for r in conn.execute(
                    "SELECT id,face_value FROM denominations"
                    " WHERE currency_code=?", (ccy,))}
        conn.close()

        # 每请求连接由 Api(db_path) 指向临时库
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.api = Api(self.db_path)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.api = ApiClient(f"http://127.0.0.1:{self.port}")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)


class TestHttpFlow(HttpTestBase):
    def test_full_happy_path(self):
        did = self.denoms["USD"]["100"]
        st, meta = self.api.call("/api/meta")
        self.assertEqual(st, 200)
        self.assertTrue(any(t["username"] == "wang_wu" for t in meta["tellers"]))

        st, r = self.api.call("/api/appointments", {
            "customer": {"name": "接口客户", "id_type": "护照",
                         "id_no": "W0001", "phone": "138"},
            "currency": "USD", "branch_code": "BR001",
            "valid_hours": 24, "items": {str(did): 4}}, "zhang_san")
        self.assertEqual(st, 200)
        code = r["appointment"]["code"]

        st, r = self.api.call(f"/api/appointments/{code}/confirm", {}, "zhang_san")
        self.assertEqual(st, 200)
        self.assertTrue(r["appointment"]["amount_cny_display"])

        st, r = self.api.call("/api/deliver",
                              {"code": code, "counted": {str(did): 4}}, "zhang_san")
        self.assertEqual(st, 200)
        self.assertEqual(r["appointment"]["status"], "DELIVERED")

    def test_permission_and_status_codes(self):
        # 柜员发布汇率 → 403
        st, r = self.api.call("/api/fx/publish",
                              {"currency": "USD", "rate": "8.0"}, "zhang_san")
        self.assertEqual(st, 403)
        self.assertEqual(r["code"], "permission_denied")

        # 库存不足 → 409
        did = self.denoms["GBP"]["50"]
        st, r = self.api.call("/api/appointments", {
            "customer": {"name": "超额", "id_type": "护照", "id_no": "W9"},
            "currency": "GBP", "branch_code": "BR001",
            "items": {str(did): 999}}, "zhang_san")
        self.assertEqual(st, 409)
        self.assertEqual(r["code"], "inventory_shortage")

    def test_static_assets(self):
        for path in ("/", "/static/styles.css", "/static/app.js"):
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(len(r.read()) > 100)


if __name__ == "__main__":
    unittest.main()
