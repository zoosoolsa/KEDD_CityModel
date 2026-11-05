"""브이월드 오픈API: 인증키 보관 + 도로명주소건물(LT_C_SPBD)·연속지적도 도로 필지(LP_PA_CBND_BUBUN) 수집.

- 키는 운영체제 키체인(macOS 키체인 / Windows 자격 증명 관리자)에 저장, 실패하면 홈 폴더 파일(권한 600)
- 데이터 API 제한: 요청당 최대 1,000건, 범위 2km² 이하 → 1.4km 격자로 나눠 요청하고 칸마다 캐시
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
import numpy as np
from .util import Reporter

SERVICE = "KEDD-CityModel"
USER = "vworld-api-key"
DOMAIN = "http://localhost:47380"
DATA_URL = "https://api.vworld.kr/req/data"
BOX = 1400.0                 # m (1.96km²)
CACHE_DAYS = 35
_FALLBACK = os.path.join(os.path.expanduser("~"), ".kedd_citymodel_key")


def get_key():
    try:
        import keyring
        k = keyring.get_password(SERVICE, USER)
        if k:
            return k
    except Exception:
        pass
    if os.path.exists(_FALLBACK):
        return open(_FALLBACK, encoding="utf-8").read().strip()
    return ""


def set_key(k):
    k = (k or "").strip()
    try:
        import keyring
        if k:
            keyring.set_password(SERVICE, USER, k)
        else:
            try:
                keyring.delete_password(SERVICE, USER)
            except Exception:
                pass
        if os.path.exists(_FALLBACK):
            os.remove(_FALLBACK)
        return "keychain"
    except Exception:
        with open(_FALLBACK, "w", encoding="utf-8") as f:
            f.write(k)
        try:
            os.chmod(_FALLBACK, 0o600)
        except Exception:
            pass
        return "file"


class ApiError(Exception):
    pass


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def get_feature(key, data, box5186, page=1, size=1000, timeout=30):
    q = dict(service="data", version="2.0", request="GetFeature", key=key, domain=DOMAIN, format="json",
             data=data, size=str(size), page=str(page), geometry="true", attribute="true", crs="EPSG:5186",
             geomFilter="BOX(%.1f,%.1f,%.1f,%.1f)" % tuple(box5186))
    req = urllib.request.Request(DATA_URL + "?" + urllib.parse.urlencode(q), headers={"User-Agent": "KEDD-CityModel"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        d = json.loads(r.read().decode("utf-8"))
    res = d.get("response", {})
    st = res.get("status")
    if st == "ERROR":
        e = res.get("error", {})
        raise ApiError(f"{e.get('code')}: {e.get('text')}")
    if st == "NOT_FOUND":
        return [], 1
    feats = res.get("result", {}).get("featureCollection", {}).get("features", [])
    return feats, int(res.get("page", {}).get("total", 1))


def test_key(key):
    """키가 이 앱 주소(localhost:47380)로 동작하는지 확인 → (ok, 메시지)"""
    try:
        get_feature(key, "LT_C_SPBD", (199000, 545000, 199500, 545500), size=1)
        return True, "인증키 확인 완료"
    except ApiError as e:
        if "INCORRECT_KEY" in str(e):
            return False, "인증키의 서비스 URL 이 http://localhost:47380 이 아닙니다"
        return False, str(e)
    except Exception as e:
        return False, f"접속 실패: {e}"


def _parse_spbd(f):
    g = f.get("geometry") or {}
    pr = f.get("properties") or {}
    try:
        fl = int(float(pr.get("gro_flo_co") or 0))
    except ValueError:
        fl = 0
    out = []
    polys = g.get("coordinates", []) if g.get("type") == "MultiPolygon" else [g.get("coordinates", [])]
    for poly in polys:
        if poly and len(poly[0]) >= 4:
            out.append([fl, [[round(x, 2), round(y, 2)] for x, y in poly[0]]])
    return out


def jimok(jibun):
    """'123-4도', '산 12 임' → '도' / '임' (지번 끝의 한글 한 글자)"""
    s = (jibun or "").strip()
    for ch in reversed(s):
        if "\uac00" <= ch <= "\ud7a3":
            return ch
        if not ch.isspace():
            break
    return ""


ROAD_JIMOK = {"도"}


def _parse_cbnd(f):
    """연속지적도 필지 → 지목이 도로(도)인 것만 [지목, [외곽, 구멍...]]"""
    pr = f.get("properties") or {}
    jm = jimok(pr.get("jibun"))
    if jm not in ROAD_JIMOK:
        return []
    g = f.get("geometry") or {}
    polys = g.get("coordinates", []) if g.get("type") == "MultiPolygon" else [g.get("coordinates", [])]
    out = []
    for poly in polys:
        if poly and len(poly[0]) >= 4:
            out.append([jm, [[[round(x, 2), round(y, 2)] for x, y in ring] for ring in poly if len(ring) >= 4]])
    return out


def _cell_fetch(key, cell, cache_dir, data="LT_C_SPBD", parse=_parse_spbd):
    p = os.path.join(cache_dir, f"{cell[0]}_{cell[1]}.json")
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < CACHE_DAYS * 86400:
        return json.load(open(p, encoding="utf-8"))
    box = (cell[0] * BOX, cell[1] * BOX, (cell[0] + 1) * BOX, (cell[1] + 1) * BOX)
    out = []
    page, total = 1, 1
    while page <= total:
        for attempt in range(3):
            try:
                feats, total = get_feature(key, data, box, page=page)
                break
            except ApiError:
                raise
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        for f in feats:
            out.extend(parse(f))
        page += 1
    tmp = p + ".tmp"
    json.dump(out, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, p)
    return out


def _cells(tm_bounds):
    return [(i, j) for i in range(int(tm_bounds[0] // BOX), int(tm_bounds[2] // BOX) + 1)
            for j in range(int(tm_bounds[1] // BOX), int(tm_bounds[3] // BOX) + 1)]


def fetch_road_parcels(key, tm_bounds, cache_dir, rep: Reporter, workers=6):
    """범위 안 지적도 도로 필지 → (폴리곤 목록 [ [외곽, 구멍...] (EPSG:5186) ], 받은 칸 집합)"""
    os.makedirs(cache_dir, exist_ok=True)
    cells = _cells(tm_bounds)
    cached = sum(1 for c in cells if os.path.exists(os.path.join(cache_dir, f"{c[0]}_{c[1]}.json")))
    rep.log(f"   브이월드 API 연속지적도(도로 필지): {len(cells)}칸 (캐시 {cached}칸)")
    polys, ok, stop, done = [], set(), None, 0
    seen = set()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_cell_fetch, key, c, cache_dir, "LP_PA_CBND_BUBUN", _parse_cbnd): c for c in cells}
        for f in cf.as_completed(futs):
            done += 1
            if done % 10 == 0:
                rep.progress(done / len(cells))
            try:
                for _, rings in f.result():
                    k = (round(rings[0][0][0], 1), round(rings[0][0][1], 1), len(rings[0]))
                    if k not in seen:
                        seen.add(k)
                        polys.append(rings)
                ok.add(futs[f])
            except ApiError as e:
                stop = str(e)
                for g in futs:
                    g.cancel()
                break
            except Exception as e:
                stop = stop or str(e)
    if stop:
        rep.log(f"   ⚠ 지적도 일부 실패 ({stop}) — 받지 못한 곳은 OSM 도로 사용")
    rep.log(f"   → 도로 필지 {len(polys):,}개")
    return polys, ok


def fetch_spbd(key, tm_bounds, cache_dir, rep: Reporter, workers=6):
    """범위 안 도로명주소건물 → list of (층수, ring ndarray(EPSG:5186)). 오류 시 가능한 만큼만."""
    os.makedirs(cache_dir, exist_ok=True)
    cells = _cells(tm_bounds)
    cached = sum(1 for c in cells if os.path.exists(os.path.join(cache_dir, f"{c[0]}_{c[1]}.json")))
    rep.log(f"   브이월드 API 도로명주소건물: {len(cells)}칸 (캐시 {cached}칸)")
    res, done, stop = [], 0, None
    seen = set()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_cell_fetch, key, c, cache_dir): c for c in cells}
        for f in cf.as_completed(futs):
            done += 1
            if done % 20 == 0:
                rep.progress(done / len(cells))
            try:
                for fl, ring in f.result():
                    k = (round(ring[0][0], 1), round(ring[0][1], 1), len(ring))
                    if k not in seen:          # 칸 경계에 걸친 건물 중복 제거
                        seen.add(k)
                        res.append((fl, np.asarray(ring, float)))
            except ApiError as e:
                stop = str(e)
                for g in futs:
                    g.cancel()
                break
            except Exception as e:
                stop = stop or str(e)
    if stop:
        rep.log(f"   ⚠ 브이월드 API 일부 실패 ({stop}) — 받은 만큼만 사용")
    rep.log(f"   → 도로명주소건물 {len(res):,}동 (층수 있음 {sum(1 for f, _ in res if f > 0):,})")
    return res
