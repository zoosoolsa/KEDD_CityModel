"""앱 내장 서버 (http://localhost:47380): 브이월드 3D 지도 페이지 제공 + 시점 수신.
브이월드 인증키의 서비스 URL 을 http://localhost:47380 으로 발급받아야 지도가 열린다."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 47380
DOMAIN = f"http://localhost:{PORT}"


def _page_template():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "map.html")
    if not os.path.exists(p):
        p = os.path.join(base, "citymodel", "map.html")
    return open(p, encoding="utf-8").read()


class MapServer:
    def __init__(self, get_key, on_capture, get_init=None):
        """get_key(): 인증키, on_capture(cam) -> 메시지 문자열, get_init(): 처음 보여줄 위치 dict"""
        self.get_key = get_key
        self.on_capture = on_capture
        self.get_init = get_init or (lambda: dict(lon=126.99, lat=37.50, alt=9000, heading=0, tilt=-40))
        self.httpd = None

    def start(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8"):
                b = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.split("?")[0] in ("/", "/map.html"):
                    key = srv.get_key() or ""
                    if not key:
                        return self._send(200, "<meta charset=utf-8><h3>브이월드 인증키가 없습니다.</h3>앱의 설정에서 인증키를 입력하세요.")
                    html = (_page_template().replace("__APIKEY__", key).replace("__DOMAIN__", DOMAIN)
                            .replace("__INIT__", json.dumps(srv.get_init())))
                    return self._send(200, html)
                self._send(404, "not found", "text/plain")

            def do_POST(self):
                if self.path == "/capture":
                    n = int(self.headers.get("Content-Length") or 0)
                    try:
                        cam = json.loads(self.rfile.read(n).decode("utf-8"))
                        m = srv.on_capture(cam) or "앱에서 확인하세요."
                        return self._send(200, json.dumps(dict(ok=True, message=m), ensure_ascii=False), "application/json")
                    except Exception as e:
                        return self._send(200, json.dumps(dict(ok=False, message=f"오류: {e}"), ensure_ascii=False), "application/json")
                self._send(404, "not found", "text/plain")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", PORT), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def url(self):
        return DOMAIN + "/"

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
