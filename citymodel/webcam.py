"""웹 지도 URL → 카메라 시점.

지원
  구글 어스 웹  https://earth.google.com/web/@37.51,127.10,35a,1500d,35y,45h,60t,0r
               (위도,경도 = 바라보는 지점 / a = 그 지점 고도 / d = 카메라까지 거리 / y = 화각 / h = 방위 / t = 기울기)
  구글 지도     https://www.google.com/maps/@37.51,127.10,1200a,35y,45h,60t/...   (3D)
               https://www.google.com/maps/@37.51,127.10,15z  /  ...,800m/data=!3m1!1e3   (2D·위성)
  네이버 지도   https://map.naver.com/p?c=127.0276,37.4979,15,60,30,0,dh   (경도,위도,줌,기울기,회전)
               (EPSG:3857 미터 좌표 형식도 자동 인식)
  카카오맵      https://map.kakao.com/?urlX=497044&urlY=1127221&urlLevel=3   (위에서 내려다본 시점)
  브이월드      URL 의 lon/lat(x/y) 와 alt·heading·tilt 류 파라미터를 최대한 해석
  직접 입력     위도,경도,거리,방위,기울기[,화각]   예) 37.51,127.10,3000,20,55
반환: dict(lat, lon, range(m), heading(°, 북=0 시계방향), tilt(°, 0=수직 내려다봄), fov(° 세로), source)
"""
import math
import re
from urllib.parse import urlparse, parse_qs, unquote
from pyproj import Transformer

_3857 = Transformer.from_crs(3857, 4326, always_xy=True)
_5181 = Transformer.from_crs(5181, 4326, always_xy=True)
VIEW_PX = 900          # 2D 지도 줌 → 거리 환산용 기준 화면 높이(px)
DEFAULT_FOV = 35.0


def _zoom_to_range(zoom, lat, fov=DEFAULT_FOV):
    mpp = 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)
    visible = mpp * VIEW_PX
    return visible / 2 / math.tan(math.radians(fov) / 2)


def _num(s):
    return float(re.sub(r"[^0-9eE+\-.]", "", s))


def parse_url(url: str) -> dict:
    u = url.strip()
    if not u:
        raise ValueError("빈 URL")
    # --- 직접 입력 ---
    if re.fullmatch(r"[\d\s.,\-]+", u):
        v = [float(x) for x in u.split(",") if x.strip()]
        if len(v) < 5:
            raise ValueError("직접 입력은 위도,경도,거리,방위,기울기[,화각] 형식입니다")
        return dict(lat=v[0], lon=v[1], range=v[2], heading=v[3], tilt=v[4],
                    fov=v[5] if len(v) > 5 else DEFAULT_FOV, source="직접 입력")
    u = unquote(u)
    pr = urlparse(u if "://" in u else "https://" + u)
    host = pr.netloc.lower()
    q = {k.lower(): v[0] for k, v in parse_qs(pr.query).items()}

    # --- 구글 (지도/어스): @lat,lng,....  ---
    m = re.search(r"@(-?\d+\.?\d*),(-?\d+\.?\d*),([^/?]*)", u)
    if m and ("google" in host or "goo.gl" in host):
        lat, lon = float(m.group(1)), float(m.group(2))
        tok = {}
        for t in m.group(3).split(","):
            mm = re.fullmatch(r"(-?\d+\.?\d*)([a-z])", t.strip())
            if mm:
                tok[mm.group(2)] = float(mm.group(1))
        fov = tok.get("y", DEFAULT_FOV)
        h = tok.get("h", 0.0)
        tilt = tok.get("t", 0.0)
        if "d" in tok:                                   # 구글 어스: d = 거리
            rng = tok["d"]
            src = "구글 어스"
        elif "a" in tok:                                 # 구글 지도 3D: a = 카메라 거리(고도)
            rng = tok["a"] / max(math.cos(math.radians(tilt)), 0.15) if tilt < 80 else tok["a"]
            src = "구글 지도 3D"
        elif "m" in tok:                                 # 위성 2D: m = 화면 세로 범위(m)
            rng = tok["m"] / 2 / math.tan(math.radians(fov) / 2)
            src = "구글 지도"
        elif "z" in tok:
            rng = _zoom_to_range(tok["z"], lat, fov)
            src = "구글 지도"
        else:
            raise ValueError("구글 URL 에서 거리/줌 값을 찾지 못했습니다")
        return dict(lat=lat, lon=lon, range=rng, heading=h, tilt=tilt, fov=fov, source=src)

    # --- 네이버 ---
    if "naver" in host:
        c = q.get("c")
        if not c:
            m = re.search(r"[?&]c=([^&]+)", u)
            c = m.group(1) if m else None
        if not c:
            raise ValueError("네이버 URL 에 c= 파라미터가 없습니다 (지도 화면의 주소창 URL을 복사하세요)")
        v = [x for x in c.split(",")]
        nums = []
        for x in v:
            try:
                nums.append(float(x))
            except ValueError:
                pass
        if len(nums) < 3:
            raise ValueError("네이버 c= 값 형식을 알 수 없습니다: " + c)
        x, y, zoom = nums[0], nums[1], nums[2]
        tilt = nums[3] if len(nums) > 3 else 0.0
        rot = nums[4] if len(nums) > 4 else 0.0
        if abs(x) > 1000:                                 # EPSG:3857 미터
            x, y = _3857.transform(x, y)
        lon, lat = (x, y) if x > y else (y, x)
        return dict(lat=lat, lon=lon, range=_zoom_to_range(zoom, lat), heading=rot % 360, tilt=tilt,
                    fov=DEFAULT_FOV, source="네이버 지도")

    # --- 카카오 ---
    if "kakao" in host or "daum" in host:
        if "urlx" in q and "urly" in q:
            X, Y = float(q["urlx"]) / 2.5, float(q["urly"]) / 2.5      # WCONGNAMUL → EPSG:5181
            lon, lat = _5181.transform(X, Y)
            level = float(q.get("urllevel", 3))
            zoom = 20 - level
            return dict(lat=lat, lon=lon, range=_zoom_to_range(zoom, lat), heading=0.0, tilt=0.0,
                        fov=DEFAULT_FOV, source="카카오맵")
        raise ValueError("카카오맵 URL 에 urlX/urlY 가 없습니다 (지도 공유 > URL 복사 사용)")

    # --- 브이월드 / 기타: 파라미터 이름으로 추정 ---
    def pick(*keys):
        for k in keys:
            if k in q:
                try:
                    return _num(q[k])
                except Exception:
                    pass
        return None
    lon = pick("lon", "lng", "longitude", "x", "cx", "centerx")
    lat = pick("lat", "latitude", "y", "cy", "centery")
    if lon is None or lat is None:
        m = re.search(r"(1[23]\d\.\d+)[,/ ]+(3[3-8]\.\d+)", u) or re.search(r"(3[3-8]\.\d+)[,/ ]+(1[23]\d\.\d+)", u)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            lon, lat = (a, b) if a > b else (b, a)
    if lon is None or lat is None:
        raise ValueError("URL 에서 좌표를 찾지 못했습니다. 지원 형식: 구글 지도/어스, 네이버, 카카오, 브이월드 (또는 위도,경도,거리,방위,기울기)")
    if abs(lon) > 1000:                                   # 미터 좌표 → 3857 가정
        lon, lat = _3857.transform(lon, lat)
    alt = pick("alt", "altitude", "height", "z", "range", "dist", "distance")
    zoom = pick("zoom", "level", "lvl", "zl")
    rng = alt if alt else (_zoom_to_range(zoom, lat) if zoom else 3000.0)
    return dict(lat=lat, lon=lon, range=rng, heading=(pick("heading", "head", "yaw", "dir", "rot", "rotation") or 0.0) % 360,
                tilt=pick("tilt", "pitch", "ang", "angle") or 0.0, fov=pick("fov") or DEFAULT_FOV,
                source="브이월드" if "vworld" in host else host or "URL")


def camera_from_view(v, terrain):
    """웹 시점 → Blender 카메라 (로컬 좌표). terrain: util.Terrain (exag 포함)"""
    from .util import to_tm
    X, Y = to_tm(v["lon"], v["lat"])
    cx, cy = terrain.origin
    tx, ty = X - cx, Y - cy
    tz = float(terrain.ground([tx], [ty])[0])
    t = math.radians(max(0.0, min(v["tilt"], 89.0)))
    h = math.radians(v["heading"])
    r = v["range"]
    loc = [tx - r * math.sin(t) * math.sin(h), ty - r * math.sin(t) * math.cos(h), tz + r * math.cos(t)]
    return dict(location=loc, rotation=[t, 0.0, -h], fov=v["fov"], source=v["source"])
