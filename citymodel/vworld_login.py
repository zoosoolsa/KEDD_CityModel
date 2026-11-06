"""브이월드 로그인 세션으로 건물 데이터(zip)를 앱이 직접 받는다.

- 비밀번호는 앱이 보지도 저장하지도 않는다. 사용자가 앱 안의 브이월드 창에서 직접 로그인하고,
  앱은 그 창의 로그인 쿠키만 이 컴퓨터의 개인 파일(권한 600)에 보관한다.
- 브라우저 탭을 열지 않는다. 로그인이 필요하면 창을 하나만 띄우고, 한 번 시도해서 안 되면 멈춘다.
- 로그인 창은 별도 프로세스로 띄운다 (tkinter 와 웹 창은 둘 다 메인 스레드가 필요해서).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

from .library import VWORLD_PAGE

HOME = os.path.join(os.path.expanduser("~"), ".kedd_citymodel")
SESSION_FILE = os.path.join(HOME, "vworld_session.json")
WEB_STORAGE = os.path.join(HOME, "webview")
# 브이월드 통합로그인 화면으로 바로 (로그인 후 메인으로 돌아옴 → 감지해서 창 닫음)
LOGIN_URL = "https://www.vworld.kr/oidc/auth?endPoint=%2Fv4po_main.do&acrValues=3"
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")


class LoginRequired(Exception):
    pass


# ------------------------------------------------------------------ 세션
def load_session():
    try:
        d = json.load(open(SESSION_FILE, encoding="utf-8"))
        return d if d.get("cookie") else None
    except Exception:
        return None


def save_session(cookie, ua):
    os.makedirs(HOME, exist_ok=True)
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"cookie": cookie, "ua": ua, "t": time.time()}, f)


def clear_session():
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass


def _ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _open(sess, url, rng=None, timeout=30):
    h = {"User-Agent": (sess or {}).get("ua") or DEFAULT_UA, "Referer": VWORLD_PAGE}
    if sess and sess.get("cookie"):
        h["Cookie"] = sess["cookie"]
    if rng:
        h["Range"] = rng
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout, context=_ctx())


def probe(sess, url):
    """이 세션으로 실제 zip 이 받아지는지 (앞 4바이트만 읽음)"""
    if not sess:
        return False
    try:
        with _open(sess, url, rng="bytes=0-3", timeout=20) as r:
            return r.read(4) == b"PK\x03\x04"
    except Exception:
        return False


def download(sess, url, dest, progress=None, stop=lambda: False):
    """zip 을 dest 로 받기. 로그인 페이지가 오면 LoginRequired."""
    tmp = dest + ".part"
    with _open(sess, url, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        head = r.read(4)
        if head != b"PK\x03\x04":
            raise LoginRequired("브이월드 로그인이 필요합니다")
        got = 4
        last = 0.0
        with open(tmp, "wb") as f:
            f.write(head)
            while True:
                if stop():
                    raise InterruptedError("중지됨")
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                got += len(b)
                if progress and time.time() - last > 0.5:
                    last = time.time()
                    progress(got, total)
    if total and got < total:
        raise IOError(f"받다가 끊겼습니다 ({got:,}/{total:,} bytes)")
    os.replace(tmp, dest)
    if progress:
        progress(got, total or got)
    return dest


# ------------------------------------------------------------------ 로그인 창 (별도 프로세스)
def _self_cmd():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "citymodel"]


def run_login(test_url, timeout_min=15):
    """로그인 창을 띄우고 (별도 프로세스) 로그인되면 True. 창을 닫거나 시간이 지나면 False."""
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        p = subprocess.run(_self_cmd() + ["--vworld-login", test_url], env=env, timeout=timeout_min * 60,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "로그인 창 시간이 지났습니다"
    except Exception as e:
        return False, f"로그인 창을 열지 못했습니다: {e}"
    out = (p.stdout or "") + (p.stderr or "")
    if "KEDD_LOGIN_OK" in out:
        return True, ""
    if "KEDD_LOGIN_NOWEBVIEW" in out:
        return False, "이 컴퓨터에서 앱 안 웹 창을 쓸 수 없습니다"
    return False, "로그인 창을 닫았습니다"


BANNER_JS = r"""
(function(){
  if (document.getElementById('kedd-banner')) return;
  var d = document.createElement('div');
  d.id = 'kedd-banner';
  d.textContent = '로그인하면 이 창은 자동으로 닫히고, 앱이 건물 데이터를 받기 시작합니다.';
  d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;background:#1f6feb;color:#fff;' +
                    'font:600 14px -apple-system,system-ui,sans-serif;padding:10px 16px;text-align:center;';
  document.body.appendChild(d);
})();
"""


def login_main(test_url):
    """--vworld-login: 브이월드 창을 띄우고, 로그인되면 쿠키를 저장하고 닫는다."""
    try:
        import webview
    except Exception:
        print("KEDD_LOGIN_NOWEBVIEW", flush=True)
        return 2
    os.makedirs(WEB_STORAGE, exist_ok=True)
    state = {"ok": False}
    win = webview.create_window("브이월드 로그인 — KEDD CityModel", LOGIN_URL, width=980, height=860)

    def cookie_header():
        parts = []
        try:
            for c in win.get_cookies() or []:
                for k, m in c.items():
                    parts.append(f"{k}={m.value}")
        except Exception:
            pass
        return "; ".join(parts)

    def watch():
        ua = DEFAULT_UA
        try:
            ua = win.evaluate_js("navigator.userAgent") or DEFAULT_UA
        except Exception:
            pass
        last_try = 0.0
        while not state["ok"]:
            time.sleep(2)
            try:
                if "vworld.kr" in (win.get_current_url() or ""):
                    win.evaluate_js(BANNER_JS)
                txt = win.evaluate_js("document.body ? document.body.innerText : ''") or ""
            except Exception:
                txt = ""
            if "로그아웃" not in txt and time.time() - last_try < 30:
                continue                         # 로그인 전: 30초마다만 확인 (서버 부담 최소)
            last_try = time.time()
            ck = cookie_header()
            sess = {"cookie": ck, "ua": ua}
            if ck and probe(sess, test_url):
                save_session(ck, ua)
                state["ok"] = True
                print("KEDD_LOGIN_OK", flush=True)
                try:
                    win.destroy()
                except Exception:
                    pass
                return

    webview.start(watch, private_mode=False, storage_path=WEB_STORAGE)
    return 0 if state["ok"] else 1
