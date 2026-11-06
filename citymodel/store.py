"""타일 저장소 + 프로젝트(장면).

작업폴더/
  store.json                     전체 설정 (타일 크기, 지형 과장, 버전)
  tiles/<ix>_<iy>/               4km 타일 (EPSG:5186, 남서 모서리 = ix*T, iy*T)
      terrain.npz  landcover.png(R녹지 G물)  bld.npz  rd_major.npz  rd_minor.npz(지형에 얹은 도로)  br.npz  meta.json
  projects/<이름>/
      <이름>.blend  project.json (타일 목록·버전, 카메라 목록, 원점)
"""
import datetime as dt
import glob
import json
import math
import os
import shutil
import numpy as np
from PIL import Image
from .util import MeshAcc, Terrain, Reporter, to_tm, to_ll

Image.MAX_IMAGE_PIXELS = None
TILE = 4000.0
TILE_VERSION = 10          # 생성 방식이 바뀌면 올림 → 모든 타일 재생성
KINDS = ("bld", "rd_major", "rd_minor", "br")
# 종류별 평면 변 길이 상한 (m): 도로 조각은 25m 삼각형 안, 다리는 10m 간격, 건물은 큰 공장·역사도 수백 m
MAX_EDGE = {"bld": 700.0, "rd_major": 40.0, "rd_minor": 40.0, "br": 80.0}


# ------------------------------------------------------------------ 기본
def tile_key(X, Y, T=TILE):
    return (int(math.floor(X / T)), int(math.floor(Y / T)))


def tile_bounds(k, T=TILE):
    return (k[0] * T, k[1] * T, (k[0] + 1) * T, (k[1] + 1) * T)


def tiles_in_tm_bounds(b, T=TILE):
    k0, k1 = tile_key(b[0], b[1], T), tile_key(b[2] - 1e-6, b[3] - 1e-6, T)
    return {(i, j) for i in range(k0[0], k1[0] + 1) for j in range(k0[1], k1[1] + 1)}


def tiles_for_bbox_ll(bbox):
    lon0, lat0, lon1, lat1 = bbox
    xs, ys = to_tm(np.array([lon0, lon1, lon0, lon1]), np.array([lat0, lat0, lat1, lat1]))
    return tiles_in_tm_bounds((min(xs), min(ys), max(xs), max(ys)))


def tiles_for_polygon_tm(pts, buffer=300.0, T=TILE):
    from shapely.geometry import Polygon, MultiPoint, box
    g = MultiPoint([tuple(p) for p in pts]).convex_hull.buffer(buffer)
    out = set()
    for k in tiles_in_tm_bounds(g.bounds, T):
        if g.intersects(box(*tile_bounds(k, T))):
            out.add(k)
    return out


def components(keys):
    """8-이웃 연결 묶음"""
    keys = set(keys)
    out = []
    while keys:
        stack = [keys.pop()]
        comp = set(stack)
        while stack:
            i, j = stack.pop()
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    n = (i + di, j + dj)
                    if n in keys:
                        keys.remove(n)
                        comp.add(n)
                        stack.append(n)
        out.append(comp)
    return out


class TileSink:
    """빌더가 만든 요소를 중심점 기준으로 타일별 MeshAcc 에 분배"""

    def __init__(self, keys, origin, T=TILE):
        self.keys = set(keys)
        self.cx, self.cy = origin
        self.T = T
        self.accs = {}

    def get(self, kind, lx, ly):
        k = tile_key(lx + self.cx, ly + self.cy, self.T)
        if k not in self.keys:
            return None
        a = self.accs.get((k, kind))
        if a is None:
            a = self.accs[(k, kind)] = MeshAcc(max_edge=MAX_EDGE.get(kind))
        return a

    def dropped(self):
        return sum(a.dropped for a in self.accs.values())

    def save(self, store):
        tot = 0
        for k in self.keys:
            d = store.tile_dir(k)
            os.makedirs(d, exist_ok=True)
            off = (self.cx - k[0] * self.T, self.cy - k[1] * self.T)   # 영역 로컬 → 타일 로컬
            for kind in KINDS:
                a = self.accs.get((k, kind))
                p = os.path.join(d, kind + ".npz")
                if a is None:
                    if os.path.exists(p):
                        os.remove(p)
                    continue
                tot += a.save(p, offset=off)
        return tot


# ------------------------------------------------------------------ 저장소
class Store:
    def __init__(self, root, exag=1.6, res=25.0):
        self.root = root
        os.makedirs(os.path.join(root, "tiles"), exist_ok=True)
        os.makedirs(os.path.join(root, "projects"), exist_ok=True)
        p = os.path.join(root, "store.json")
        if os.path.exists(p):
            self.cfg = json.load(open(p, encoding="utf-8"))
        else:
            self.cfg = dict(tile=TILE, exag=exag, res=res, created=dt.date.today().isoformat())
            self._save_cfg()
        self._tcache = {}

    def _save_cfg(self):
        json.dump(self.cfg, open(os.path.join(self.root, "store.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    @property
    def exag(self):
        return float(self.cfg["exag"])

    @property
    def res(self):
        return float(self.cfg["res"])

    def set_exag(self, exag):
        """지형 과장 변경 → 모든 타일 다시 만들어야 함"""
        if abs(exag - self.exag) > 1e-6:
            self.cfg["exag"] = exag
            self._save_cfg()
            return True
        return False

    def tile_dir(self, k):
        return os.path.join(self.root, "tiles", f"{k[0]}_{k[1]}")

    def tile_meta(self, k):
        p = os.path.join(self.tile_dir(k), "meta.json")
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    def all_tiles(self):
        out = set()
        for d in glob.glob(os.path.join(self.root, "tiles", "*_*")):
            try:
                i, j = os.path.basename(d).split("_")
                if os.path.exists(os.path.join(d, "meta.json")):
                    out.add((int(i), int(j)))
            except ValueError:
                pass
        return out

    # ---- 소스 버전 ----
    @staticmethod
    def wanted_sources(k, gis_scan_meta, osm_ver, extra=None):
        """타일 k 에 필요한 소스 버전 dict"""
        b = tile_bounds(k)
        gis = {}
        for code, (date, bounds) in gis_scan_meta.items():
            if not (bounds[2] < b[0] or bounds[0] > b[2] or bounds[3] < b[1] or bounds[1] > b[3]):
                gis[code] = date
        d = dict(engine=TILE_VERSION, gis=gis, osm=osm_ver, dem="cop30")
        if extra:
            d.update({k2: v for k2, v in extra.items() if v})   # 꺼진 옵션은 기록하지 않음
        return d

    def tile_state(self, k, wanted):
        m = self.tile_meta(k)
        if not m:
            return "missing"
        have = dict(m.get("sources") or {})
        for k2 in list(have):                    # 추가 정보로 만든 타일은 옵션을 꺼도 그대로 사용
            if k2 not in wanted and k2 not in ("engine", "gis", "osm", "dem"):
                have.pop(k2)
        if have != wanted or abs(m.get("exag", 0) - self.exag) > 1e-6:
            return "stale"
        return "ok"

    # ---- 지면 높이 (카메라 계산용) ----
    def _tile_terrain(self, k):
        if k not in self._tcache:
            p = os.path.join(self.tile_dir(k), "terrain.npz")
            self._tcache[k] = np.load(p) if os.path.exists(p) else None
        return self._tcache[k]

    def ground_at(self, X, Y, exag=True):
        k = tile_key(X, Y)
        t = self._tile_terrain(k)
        if t is None:
            return 0.0
        z = t["z"]
        res = float(t["res"])
        H, W = z.shape
        fx = min(max((X - k[0] * TILE) / res, 0), W - 1.001)
        fy = min(max(((k[1] + 1) * TILE - Y) / res, 0), H - 1.001)
        c, r = int(fx), int(fy)
        dx, dy = fx - c, fy - r
        v = (z[r, c] * (1 - dx) * (1 - dy) + z[r, c + 1] * dx * (1 - dy) + z[r + 1, c] * (1 - dx) * dy + z[r + 1, c + 1] * dx * dy)
        return float(v) * (self.exag if exag else 1.0)


# ------------------------------------------------------------------ 타일 생성
def build_tiles(store: Store, keys, s, rep: Reporter, sources_for, cancel=lambda: False):
    """keys 의 타일을 생성. 연결 묶음별로 영역을 잡아 한 번에 처리 후 타일로 분할."""
    from . import dem, osmdata, landcover, buildings, roads
    from .config import Settings
    groups = components(keys)
    T = TILE
    res = store.res
    for gi, comp in enumerate(groups):
        xs = [k[0] for k in comp]
        ys = [k[1] for k in comp]
        bx0, by0, bx1, by1 = min(xs) * T, min(ys) * T, (max(xs) + 1) * T, (max(ys) + 1) * T
        margin = 2000.0
        region = (bx0 - margin, by0 - margin, bx1 + margin, by1 + margin)
        lons, lats = to_ll(np.array([region[0], region[2], region[0], region[2]]), np.array([region[1], region[1], region[3], region[3]]))
        ss = Settings(**{k: getattr(s, k) for k in s.__dataclass_fields__})
        ss.lon_min, ss.lat_min, ss.lon_max, ss.lat_max = min(lons), min(lats), max(lons), max(lats)
        ss.exag = store.exag
        ss.dem_res = res
        tag = f"[{gi + 1}/{len(groups)}] 타일 {len(comp)}개 ({(bx1 - bx0) / 1000:.0f}×{(by1 - by0) / 1000:.0f}km)"
        work = os.path.join(store.root, "_work")
        os.makedirs(work, exist_ok=True)

        rep.set_stage(*_sub(rep, gi, len(groups), 0.00, 0.15), f"{tag} 지형")
        tpath = os.path.join(work, "terrain_region.npz")
        dem.build_terrain(ss, rep, tpath, tm_bounds=region)
        n_snap = _snap_to_neighbors(store, comp, tpath)
        if n_snap:
            rep.log(f"   이미 있는 이웃 타일 {n_snap}면에 경계 높이 맞춤")
        terrain = Terrain(tpath, exag=store.exag)
        origin = terrain.origin

        rep.set_stage(*_sub(rep, gi, len(groups), 0.15, 0.35), f"{tag} OpenStreetMap")
        osm = osmdata.get_osm(ss, rep)

        rep.set_stage(*_sub(rep, gi, len(groups), 0.35, 0.40), f"{tag} 지면 텍스처 준비")
        lcs = landcover.LandcoverSource(ss, osm, rep)

        sink = TileSink(comp, origin)
        rep.set_stage(*_sub(rep, gi, len(groups), 0.45, 0.80), f"{tag} 건물")
        buildings.build_buildings(ss, osm, terrain, rep, sink)
        rep.set_stage(*_sub(rep, gi, len(groups), 0.80, 0.95), f"{tag} 도로·다리")
        roads.build_roads(ss, osm, terrain, rep, sink)

        rep.set_stage(*_sub(rep, gi, len(groups), 0.95, 1.00), f"{tag} 지면 텍스처·타일 저장")
        if sink.dropped():
            rep.log(f"   비정상 형상 {sink.dropped():,}면 제외 (원본 데이터 오류)")
        sink.save(store)
        _save_terrain_tiles(store, comp, terrain, lcs, ss.lc_res, rep)
        for k in comp:
            json.dump(dict(sources=sources_for(k), exag=store.exag, res=res, tile=T,
                           built=dt.datetime.now().isoformat(timespec="minutes")),
                      open(os.path.join(store.tile_dir(k), "meta.json"), "w", encoding="utf-8"), ensure_ascii=False)
            store._tcache.pop(k, None)
        rep.log(f"   ✔ 타일 {len(comp)}개 저장")
        if cancel():
            break


def _sub(rep, gi, n, a, b):
    lo, hi = rep.stage_root if hasattr(rep, "stage_root") else (0.0, 1.0)
    span = (hi - lo) / n
    return lo + span * (gi + a), lo + span * (gi + b)


SNAP_BLEND = 6      # 경계에서 안쪽으로 높이 차이를 줄여 가는 칸 수 (25m × 6 = 150m)
_DIRS = {(1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S"}


def _snap_to_neighbors(store, comp, tpath):
    """영역 지형의 경계를 이미 있는(이번에 만들지 않는) 이웃 타일 높이에 정확히 맞춤.
    차이는 안쪽으로 SNAP_BLEND 칸에 걸쳐 서서히 줄인다. 반환: 맞춘 경계 수"""
    d = dict(np.load(tpath))
    z = d["z"].astype(np.float64)
    res = float(d["res"])
    cx, cy = (float(v) for v in d["origin"])
    gx0, gy0 = float(d["x0"]) + cx, float(d["y0"]) + cy        # 격자 (0,0) 전역 좌표
    T = TILE
    n = int(round(T / res))
    cset = set(map(tuple, comp))
    cnt = 0
    for k in comp:
        c0 = int(round((k[0] * T - gx0) / res))
        r0 = int(round((gy0 - (k[1] + 1) * T) / res))
        for (dx, dy), side in _DIRS.items():
            nk = (k[0] + dx, k[1] + dy)
            if nk in cset:
                continue
            p = os.path.join(store.tile_dir(nk), "terrain.npz")
            if not os.path.exists(p):
                continue
            nz = np.load(p)["z"].astype(np.float64)
            if nz.shape != (n + 1, n + 1):
                continue
            if side == "E":
                diff = nz[:, 0] - z[r0:r0 + n + 1, c0 + n]
                for j in range(SNAP_BLEND + 1):
                    z[r0:r0 + n + 1, c0 + n - j] += diff * (1 - j / (SNAP_BLEND + 1))
            elif side == "W":
                diff = nz[:, n] - z[r0:r0 + n + 1, c0]
                for j in range(SNAP_BLEND + 1):
                    z[r0:r0 + n + 1, c0 + j] += diff * (1 - j / (SNAP_BLEND + 1))
            elif side == "N":
                diff = nz[n, :] - z[r0, c0:c0 + n + 1]
                for j in range(SNAP_BLEND + 1):
                    z[r0 + j, c0:c0 + n + 1] += diff * (1 - j / (SNAP_BLEND + 1))
            else:  # S
                diff = nz[0, :] - z[r0 + n, c0:c0 + n + 1]
                for j in range(SNAP_BLEND + 1):
                    z[r0 + n - j, c0:c0 + n + 1] += diff * (1 - j / (SNAP_BLEND + 1))
            cnt += 1
    if cnt:
        d["z"] = z.astype(np.float32)
        np.savez_compressed(tpath, **d)
    return cnt


def _fix_pad_with_neighbors(store, k, zpad, cset=()):
    """법선 계산용 여유 칸(zpad)을 이웃 타일의 실제 높이로 — 이웃과 음영이 이어지게.
    이웃의 zpad 도 이 타일 값으로 갱신."""
    n = zpad.shape[0] - 3
    for (dx, dy), side in _DIRS.items():
        nk = (k[0] + dx, k[1] + dy)
        if nk in cset:                     # 같이 만드는 타일은 영역 값이 이미 이어짐
            continue
        p = os.path.join(store.tile_dir(nk), "terrain.npz")
        if not os.path.exists(p):
            continue
        nd_ = dict(np.load(p))
        nz, npad = nd_["z"], nd_["zpad"].copy()
        if nz.shape != (n + 1, n + 1):
            continue
        core = zpad[1:-1, 1:-1]
        if side == "E":
            zpad[1:-1, -1] = nz[:, 1]
            npad[1:-1, 0] = core[:, n - 1]
        elif side == "W":
            zpad[1:-1, 0] = nz[:, n - 1]
            npad[1:-1, -1] = core[:, 1]
        elif side == "N":
            zpad[0, 1:-1] = nz[n - 1, :]
            npad[-1, 1:-1] = core[1, :]
        else:
            zpad[-1, 1:-1] = nz[1, :]
            npad[0, 1:-1] = core[n - 1, :]
        nd_["zpad"] = npad
        np.savez_compressed(p, **nd_)


def _save_terrain_tiles(store, comp, terrain, lcs, lres, rep=None):
    T = TILE
    res = terrain.res
    cx, cy = terrain.origin
    gx0 = terrain.x0 + cx            # 영역 꼭짓점 (0,0) 의 전역 좌표
    gy0 = terrain.y0 + cy
    n = int(round(T / res))
    for i, k in enumerate(comp):
        tx0, ty1 = k[0] * T, (k[1] + 1) * T
        c0 = int(round((tx0 - gx0) / res))
        r0 = int(round((gy0 - ty1) / res))
        # 법선 계산용으로 한 칸씩 여유(zpad)
        z = terrain.z[r0 - 1:r0 + n + 2, c0 - 1:c0 + n + 2].astype(np.float32)
        w = terrain.water[r0:r0 + n + 1, c0:c0 + n + 1]
        d = store.tile_dir(k)
        os.makedirs(d, exist_ok=True)
        _fix_pad_with_neighbors(store, k, z, set(map(tuple, comp)))
        np.savez_compressed(os.path.join(d, "terrain.npz"), z=z[1:-1, 1:-1], zpad=z, water=w, res=res)
        img = lcs.render_tile((tx0, ty1 - T, tx0 + T, ty1), terrain, lres)
        Image.fromarray(img, "RGB").save(os.path.join(d, "landcover.png"), compress_level=6)
        if rep:
            rep.progress((i + 1) / len(comp))


# ------------------------------------------------------------------ 프로젝트
class Project:
    def __init__(self, store: Store, name):
        self.store = store
        self.name = name
        self.dir = os.path.join(store.root, "projects", name)
        self.path = os.path.join(self.dir, "project.json")
        if os.path.exists(self.path):
            self.d = json.load(open(self.path, encoding="utf-8"))
        else:
            self.d = dict(tiles=[], versions={}, cameras=[], origin=None)

    @property
    def blend(self):
        return os.path.join(self.dir, f"{self.name}.blend")

    @property
    def tiles(self):
        return {tuple(t) for t in self.d["tiles"]}

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        json.dump(self.d, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def is_current(self, keys):
        """keys 가 모두 이 프로젝트 .blend 에 최신 상태로 들어 있는가"""
        if not os.path.exists(self.blend):
            return False
        have = self.tiles
        for k in keys:
            if k not in have:
                return False
            m = self.store.tile_meta(k)
            if not m or self.d["versions"].get(f"{k[0]}_{k[1]}") != m.get("built"):
                return False
        return True


def resolve_camera(store: Store, cam, origin):
    """카메라 사양 → Blender 장면 로컬 (location, rotation(rad), fov(deg)) + 메타(id·만든 시각·설명)"""
    r = _resolve_camera(store, cam, origin)
    r.update({k: cam[k] for k in ("id", "created", "label") if cam.get(k)})
    return r


def _resolve_camera(store: Store, cam, origin):
    if cam["kind"] == "fixed":        # Blender 에서 추가한 카메라 (전역 TM + 높이)
        return dict(location=[cam["X"] - origin[0], cam["Y"] - origin[1], cam["z"]], rotation=cam["rotation"],
                    fov=cam["fov"], aspect=cam.get("aspect"), name=cam.get("name"))
    if cam["kind"] == "vworld":       # 브이월드 3D 지도: 카메라 위치(타원체고) + 방향
        X, Y = to_tm(cam["lon"], cam["lat"])
        g_real = store.ground_at(X, Y, exag=False)
        z = cam["height"] - cam.get("geoid", 25.0) + g_real * (store.exag - 1.0)
        tilt = 90.0 + cam["pitch"]
        loc = [X - origin[0], Y - origin[1], z]
        rot = [math.radians(max(0.0, min(tilt, 179.0))), 0.0, -math.radians(cam["heading"])]
        return dict(location=loc, rotation=rot, fov=cam["fov"], aspect=cam.get("aspect"), name=cam.get("name"))
    if cam["kind"] == "view":         # 웹 지도 URL: 바라보는 지점 + 거리
        X, Y = to_tm(cam["lon"], cam["lat"])
        tz = store.ground_at(X, Y)
        t = math.radians(max(0.0, min(cam["tilt"], 89.0)))
        h = math.radians(cam["heading"])
        r = cam["range"]
        loc = [X - origin[0] - r * math.sin(t) * math.sin(h), Y - origin[1] - r * math.sin(t) * math.cos(h), tz + r * math.cos(t)]
        return dict(location=loc, rotation=[t, 0.0, -h], fov=cam["fov"], aspect=cam.get("aspect"), name=cam.get("name"))
    if cam["kind"] == "default":      # 범위 전체를 남쪽에서 비스듬히
        b = cam["tm_bounds"]
        W, H = b[2] - b[0], b[3] - b[1]
        M = max(W, H)
        tx, ty = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2 - 0.06 * H
        loc = [tx - origin[0], ty - 0.31 * M - origin[1], 0.17 * M]
        return dict(location=loc, rotation=[math.atan2(0.31 * M, 0.17 * M), 0.0, 0.0],
                    fov=math.degrees(2 * math.atan(12 / 30)), aspect=1.5, name=cam.get("name"))
    raise ValueError("알 수 없는 카메라 종류")


def camera_tiles(cam, store=None):
    """카메라가 보는 땅을 덮는 타일"""
    if cam["kind"] == "vworld" and cam.get("footprint"):
        lon, lat = np.array(cam["footprint"]).T
        X, Y = to_tm(lon, lat)
        cX, cY = to_tm(cam["lon"], cam["lat"])
        pts = []
        for x, y in zip(X, Y):        # 너무 먼 지평선 쪽은 25km 로 제한
            d = math.hypot(x - cX, y - cY)
            f = min(1.0, 25000.0 / d) if d > 0 else 1.0
            pts.append((cX + (x - cX) * f, cY + (y - cY) * f))
        pts.append((cX, cY))
        return tiles_for_polygon_tm(pts, buffer=500)
    if cam["kind"] in ("view", "vworld"):
        X, Y = to_tm(cam["lon"], cam["lat"])
        r = cam.get("range") or cam.get("height", 3000)
        half = max(1500.0, r * 1.2)
        if cam["kind"] == "view":
            t = math.radians(min(cam["tilt"], 80))
            h = math.radians(cam["heading"])
            # 바라보는 방향 앞쪽으로 더 넓게
            fx, fy = X + math.sin(h) * r * math.tan(t), Y + math.cos(h) * r * math.tan(t)
            return tiles_for_polygon_tm([(X - half, Y - half), (X + half, Y + half), (fx - half, fy - half), (fx + half, fy + half)])
        return tiles_for_polygon_tm([(X - half, Y - half), (X + half, Y + half)])
    if cam["kind"] == "default":
        return tiles_in_tm_bounds(cam["tm_bounds"])
    if cam["kind"] == "fixed":
        return tiles_for_polygon_tm([(cam["X"] - 1500, cam["Y"] - 1500), (cam["X"] + 1500, cam["Y"] + 1500)])
    return set()
