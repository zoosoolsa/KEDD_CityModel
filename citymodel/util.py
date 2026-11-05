"""공통 유틸: 로그/진행률, 다운로드, 좌표, 지형 샘플링, 메쉬 누적."""
import os
import ssl
import time
import urllib.request
import numpy as np
from pyproj import Transformer

CRS = 5186  # Korea 2000 / Central Belt 2010 (브이월드 데이터와 동일)
_T = Transformer.from_crs(4326, CRS, always_xy=True)
_TI = Transformer.from_crs(CRS, 4326, always_xy=True)


def to_tm(lon, lat):
    return _T.transform(lon, lat)


def to_ll(x, y):
    return _TI.transform(x, y)


class Cancelled(Exception):
    pass


class Reporter:
    """진행률/로그 콜백 래퍼. GUI 에서 log(msg), progress(0~1) 를 받아 씀."""

    def __init__(self, log=None, progress=None, cancel_flag=None):
        self._log = log or (lambda m: print(m, flush=True))
        self._progress = progress or (lambda p: None)
        self._cancel = cancel_flag or (lambda: False)
        self.stage = (0.0, 1.0)

    def log(self, msg):
        self._log(msg)

    def set_stage(self, a, b, title=None):
        self.stage = (a, b)
        if title:
            self.log(f"▶ {title}")
        self.progress(0.0)

    def progress(self, frac):
        a, b = self.stage
        self._progress(a + (b - a) * max(0.0, min(1.0, frac)))
        if self._cancel():
            raise Cancelled()


def download(url, dest, rep: Reporter, label=None, retries=3):
    """이어받기 없이 단순 다운로드 (임시파일 → 완료 시 이동). 404 면 None."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "KEDD-CityModel/1.0"})
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got, t0 = 0, time.time()
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        rep.progress(got / total)
                    if time.time() - t0 > 2:
                        t0 = time.time()
                        rep.log(f"   {label or os.path.basename(dest)}: {got/1e6:.0f}MB" + (f" / {total/1e6:.0f}MB" if total else ""))
            os.replace(tmp, dest)
            return dest
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            last = e
        except Cancelled:
            raise
        except Exception as e:
            last = e
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"다운로드 실패: {url} ({last})")


class Terrain:
    """terrain.npz (지형 격자, 셀 중심 기준) 를 읽어 높이를 bilinear 로 샘플."""

    def __init__(self, path, exag=1.0):
        t = np.load(path)
        self.z = t["z"].astype(np.float64)
        self.water = t["water"]
        self.res = float(t["res"])
        self.x0 = float(t["x0"])     # 로컬 좌표 (원점 = origin)
        self.y0 = float(t["y0"])
        self.origin = tuple(t["origin"])
        self.H, self.W = self.z.shape
        self.exag = exag

    def ground(self, x, y):
        """로컬 x,y → 지면 높이 * exag (지형 메쉬와 동일한 보간)"""
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        fx = np.clip((x - self.x0) / self.res - 0.5, 0, self.W - 1.001)
        fy = np.clip((self.y0 - y) / self.res - 0.5, 0, self.H - 1.001)
        c = fx.astype(int)
        r = fy.astype(int)
        dx = fx - c
        dy = fy - r
        z = self.z
        v = (z[r, c] * (1 - dx) * (1 - dy) + z[r, c + 1] * dx * (1 - dy)
             + z[r + 1, c] * (1 - dx) * dy + z[r + 1, c + 1] * dx * dy)
        return v * self.exag

    def is_water(self, x, y):
        c = np.clip(((np.asarray(x) - self.x0) / self.res).astype(int), 0, self.W - 1)
        r = np.clip(((self.y0 - np.asarray(y)) / self.res).astype(int), 0, self.H - 1)
        return self.water[r, c] > 0

    @property
    def extent(self):
        """로컬 (xmin, ymin, xmax, ymax)"""
        return (self.x0, self.y0 - self.H * self.res, self.x0 + self.W * self.res, self.y0)


class MeshAcc:
    """폴리곤 메쉬 누적 → npz (v, loops, ptot, mat)"""

    def __init__(self):
        self.V, self.L, self.P, self.M = [], [], [], []
        self.n = 0

    def add(self, verts, polys, mats=None):
        verts = np.asarray(verts, np.float32)
        self.V.append(verts)
        for p in polys:
            self.L.append(np.asarray(p, np.int32) + self.n)
            self.P.append(len(p))
        self.M.extend(mats if mats is not None else [0] * len(polys))
        self.n += len(verts)

    def add_prism(self, ring, bot, top):
        """ring: CCW (k,2) → 지붕 + 벽 (바닥 없음)"""
        k = len(ring)
        v = np.vstack([np.c_[ring, np.full(k, bot)], np.c_[ring, np.full(k, top)]])
        i = np.arange(k)
        j = (i + 1) % k
        polys = [list(k + i)] + [[a, b, k + b, k + a] for a, b in zip(i, j)]
        self.add(v, polys)

    def empty(self):
        return self.n == 0

    def save(self, path):
        if self.empty():
            if os.path.exists(path):
                os.remove(path)
            return 0
        np.savez_compressed(path, v=np.vstack(self.V), loops=np.concatenate(self.L),
                            ptot=np.array(self.P, np.int32), mat=np.array(self.M, np.int32))
        return self.n


def ring_area(r):
    r = np.asarray(r)
    return 0.5 * np.sum(r[:, 0] * np.roll(r[:, 1], -1) - np.roll(r[:, 0], -1) * r[:, 1])


def densify(p, step):
    out = [p[0]]
    for a, b in zip(p[:-1], p[1:]):
        L = np.hypot(*(b - a))
        k = max(1, int(np.ceil(L / step)))
        for i in range(1, k + 1):
            out.append(a + (b - a) * i / k)
    q = np.array(out)
    keep = np.r_[True, np.hypot(*np.diff(q, axis=0).T) > 0.05]
    return q[keep]


def offset_sides(p, w):
    d = np.diff(p, axis=0)
    d /= np.linalg.norm(d, axis=1)[:, None] + 1e-9
    n = np.c_[-d[:, 1], d[:, 0]]
    nv = np.vstack([n[:1], n[:-1] + n[1:], n[-1:]])
    nv /= np.linalg.norm(nv, axis=1)[:, None] + 1e-9
    return p + nv * w / 2, p - nv * w / 2
