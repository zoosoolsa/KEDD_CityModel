"""도로: 지형 삼각형에 정확히 얹은(드레이프) 면 — 지면과 높이가 항상 일치. 다리(조각 병합, 양끝 도로 높이에 맞춘 경사 상판, 교각)."""
import os
import numpy as np
import shapely
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge
from scipy import ndimage as nd
from .util import to_tm, MeshAcc, densify, offset_sides, Reporter

CHUNK = 200.0      # 도로 합치기·자르기 단위 (m, 전역 좌표 정렬 → 타일 경계와 일치)
LIFT = 0.06        # 지면 위 띄움 (m) — 같은 평면이라 아주 조금만

WID = {'motorway': 14, 'trunk': 13, 'primary': 14, 'secondary': 11, 'tertiary': 9, 'motorway_link': 8,
       'trunk_link': 8, 'primary_link': 8, 'secondary_link': 7, 'tertiary_link': 6, 'residential': 6,
       'unclassified': 6, 'living_street': 5, 'service': 4, 'pedestrian': 5, 'rail': 10}
MAJOR = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'motorway_link', 'trunk_link',
         'primary_link', 'secondary_link', 'tertiary_link'}


def _width(w):
    wd = WID.get(w['hw'], 6)
    try:
        if w['lanes'] and w['hw'] != 'rail':
            wd = max(wd, min(float(str(w['lanes']).split(';')[0]) * 3.3 + 1, 30))
    except Exception:
        pass
    return wd


def _smoothstep(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


def build_roads(s, osm, terrain, rep: Reporter, sink):
    cx, cy = terrain.origin
    off = s.bridge_end_offset
    blines, bw = [], []
    rmaj, rmin = [], []
    ways = osm["ways"]
    for k, w in enumerate(ways):
        if k % 5000 == 0:
            rep.progress(0.3 * k / max(len(ways), 1))
        lon, lat = np.array(w['pts']).T
        X, Y = to_tm(lon, lat)
        p = np.c_[X - cx, Y - cy]
        if len(p) < 2:
            continue
        if w['bridge']:
            blines.append(LineString(p))
            bw.append(_width(w))
        elif w['hw'] != 'rail':
            (rmaj if w['hw'] in MAJOR else rmin).append((LineString(p), _width(w)))

    rep.log(f"   OSM 도로 {len(rmaj):,} (주요) · {len(rmin):,} (이면)")
    osm_pool = _osm_pool(rmaj, rmin)
    cad_pool, covered = _cadastral_pool(s, terrain, osm, rep, sink)
    drape_roads(terrain, osm_pool, cad_pool, covered, sink, rep, (0.3, 0.75))

    # ---- 다리 ----
    if blines:
        merged = linemerge(MultiLineString(blines))
        merged = list(merged.geoms) if merged.geom_type == "MultiLineString" else [merged]
        tree = shapely.STRtree(blines)
        rep.log(f"   다리 구간 {len(blines):,} → 병합 {len(merged):,}")
        for mi, ml in enumerate(merged):
            if mi % 500 == 0:
                rep.progress(0.75 + 0.24 * mi / len(merged))
            idx = tree.query(ml.buffer(0.5), predicate="intersects")
            wd = float(np.median([bw[k] for k in idx])) if len(idx) else 10.0
            p = densify(np.asarray(ml.coords)[:, :2], 10.0)
            if len(p) < 2:
                continue
            brg = sink.get("br", *p.mean(0))
            if brg is None:
                continue
            n = len(p)
            sd = np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
            S = max(sd[-1], 1.0)
            g = terrain.ground(*p.T)
            zr = g + off
            base = zr[0] + (zr[-1] - zr[0]) * sd / S
            wet = terrain.is_water(*p.T)
            if wet.any():
                C = np.where(wet, 13.0, 8.0)
            elif S > 300:
                C = np.full(n, 8.0)
            else:
                C = np.full(n, max(off, 1.2))
            tgt = g + C
            kk = 6
            tgt = nd.gaussian_filter1d(nd.maximum_filter1d(tgt, 2 * kk + 1, mode="nearest"), kk, mode="nearest")
            Rr = min(150.0, S / 3.0) if C.max() > 1.2 else max(S / 2.0, 1.0)
            ramp = _smoothstep(np.minimum(sd, S - sd) / Rr)
            zt = base * (1 - ramp) + np.maximum(base, tgt) * ramp
            zt = np.maximum(zt, g + off)
            zt[0], zt[-1] = zr[0], zr[-1]
            zb = zt - (2.2 if S > 150 else 1.2)
            L, R = offset_sides(p, wd)
            V = np.vstack([np.c_[L, zt], np.c_[R, zt], np.c_[L, zb], np.c_[R, zb]])
            Lt, Rt, Lb, Rb = 0, n, 2 * n, 3 * n
            polys = []
            for i in range(n - 1):
                polys += [[Rt + i, Rt + i + 1, Lt + i + 1, Lt + i], [Lb + i, Lb + i + 1, Rb + i + 1, Rb + i],
                          [Lt + i, Lt + i + 1, Lb + i + 1, Lb + i], [Rb + i, Rb + i + 1, Rt + i + 1, Rt + i]]
            polys += [[Lt, Lb, Rb, Rt], [Lt + n - 1, Rt + n - 1, Rb + n - 1, Lb + n - 1]]
            brg.add(V, polys, [1, 0, 0, 0] * (n - 1) + [0, 0])
            step = 50.0 if S > 150 else 30.0
            for sp in np.arange(step / 2, S, step):
                i = min(np.searchsorted(sd, sp), n - 1)
                c = p[i]
                top, bot = zb[i], g[i] - 3
                if top - bot < 7:
                    continue
                d = p[min(i + 1, n - 1)] - p[max(i - 1, 0)]
                d /= np.linalg.norm(d) + 1e-9
                nn = np.array([-d[1], d[0]])
                a, b = d * 2.0, nn * wd * 0.35
                q = [c - a - b, c + a - b, c + a + b, c - a + b]
                brg.add([(*q[j], bot) for j in range(4)] + [(*q[j], top) for j in range(4)],
                        [[0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]])
    rep.progress(1.0)


# ------------------------------------------------------------------ 도로 드레이프
def _osm_pool(rmaj, rmin):
    """OSM 도로 중심선 → 도로 폭 버퍼 (폴리곤 배열, 종류 0=주요 1=이면)"""
    def buffers(items):
        if not items:
            return np.array([], dtype=object)
        lines = np.array([l for l, _ in items], dtype=object)
        wd = np.array([w for _, w in items]) / 2.0
        g = shapely.buffer(lines, wd, quad_segs=3, cap_style="round", join_style="round")
        return shapely.simplify(g, 0.2)
    bmaj, bmin = buffers(rmaj), buffers(rmin)
    return np.concatenate([bmaj, bmin]), np.r_[np.zeros(len(bmaj), int), np.ones(len(bmin), int)]


NONROAD_SKIP = {"도"}          # 이 지목은 도로로 취급 (나머지 필지는 도로가 아님)


def _cadastral_pool(s, terrain, osm, rep, sink):
    """브이월드 연속지적도 (GIS 건물과 같은 기준) — 인증키·API 사용이 켜져 있을 때.
    도로 = 칸 전체 − 도로가 아닌 필지 − 물. ('도' 필지 + 지적도에 필지가 없는 공공 도로 빈 곳)
    반환: (도로 아닌 필지 배열, 물 폴리곤 배열), 다 받은 칸 집합"""
    empty = None, set()
    if not getattr(s, "api_heights", False):
        return empty
    from . import vworld_api
    key = vworld_api.get_key()
    if not key:
        return empty
    cx, cy = terrain.origin
    ks, T = list(sink.keys), sink.T                       # 이번에 만드는 타일 범위만
    b = (min(k[0] for k in ks) * T + 1, min(k[1] for k in ks) * T + 1,
         (max(k[0] for k in ks) + 1) * T - 1, (max(k[1] for k in ks) + 1) * T - 1)
    try:
        parcels, ok = vworld_api.fetch_parcels(key, b, os.path.join(s.cache_dir, "cbnd"), rep)
    except Exception as e:
        rep.log(f"   ⚠ 지적도를 받지 못해 OSM 도로를 씁니다 ({e})")
        return empty
    if not parcels or not ok:
        return empty
    geoms = []
    for jm, rings in parcels:
        if jm in NONROAD_SKIP:
            continue
        try:
            g = shapely.Polygon(np.asarray(rings[0]) - (cx, cy), [np.asarray(r) - (cx, cy) for r in rings[1:]])
            if not g.is_valid:
                g = shapely.make_valid(g)
            geoms.append(g)
        except Exception:
            pass
    # 물 (OSM 물 폴리곤 + 지형에서 찾은 물) — 강은 필지가 없는 곳이 많아 도로로 오인하지 않게
    water = []
    from shapely import wkb
    for gb in osm.get("water", []):
        try:
            g = wkb.loads(gb)
            for pg in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
                lon, lat = np.asarray(pg.exterior.coords).T[:2]
                X, Y = to_tm(lon, lat)
                water.append(shapely.Polygon(np.c_[X - cx, Y - cy]).buffer(0))
        except Exception:
            pass
    water += _terrain_water_polys(terrain)
    rep.log(f"   지적도로 도로 영역 계산 (도로 아닌 필지 {len(geoms):,}개, 물 {len(water):,}개)")
    return (np.array(geoms, dtype=object), np.array(water, dtype=object)), ok


def _terrain_water_polys(terrain):
    """지형 물 마스크(25m) → 폴리곤 (로컬)"""
    w = terrain.water.astype(bool)
    if not w.any():
        return []
    res = terrain.res
    out = []
    # 행 단위 연속 구간 → 직사각형 (단순·충분)
    H, W = w.shape
    for r in range(H):
        row = w[r]
        if not row.any():
            continue
        d = np.diff(np.r_[0, row.astype(np.int8), 0])
        st, en = np.where(d == 1)[0], np.where(d == -1)[0]
        y1 = terrain.y0 - (r - 0.5) * res
        for a, b in zip(st, en):
            out.append(shapely.box(terrain.x0 + (a - 0.5) * res, y1 - res, terrain.x0 + (b - 0.5) * res, y1))
    if not out:
        return []
    u = shapely.union_all(np.array(out, dtype=object))
    return list(getattr(u, "geoms", [u]))


def _cad_roads(cad, box, maj_tree, maj):
    """한 조각(200m)의 지적도 도로 영역 → (주요, 이면)"""
    nonroad, water, t1, t2 = cad
    idx = t1.query(box, predicate="intersects")
    occ = shapely.union_all(nonroad[idx]) if len(idx) else None
    road = shapely.difference(box, occ) if occ is not None else box
    wi = t2.query(box, predicate="intersects") if t2 is not None else []
    if len(wi):
        road = shapely.difference(road, shapely.union_all(water[wi]))
    if road.is_empty:
        return None, None
    road = shapely.buffer(shapely.buffer(road, -0.6, join_style="mitre"), 0.6, join_style="mitre")   # 필지 사이 틈 제거
    if road.is_empty:
        return None, None
    parts = [g for g in getattr(road, "geoms", [road]) if g.geom_type == "Polygon" and g.area >= 25]
    if not parts:
        return None, None
    road = shapely.union_all(np.array(parts, dtype=object))
    mi = maj_tree.query(box, predicate="intersects") if maj_tree is not None else []
    if len(mi):
        zone = shapely.buffer(shapely.union_all(maj[mi]), 6.0)
        return shapely.intersection(road, zone), shapely.difference(road, zone)
    return None, road


def drape_roads(terrain, osm_pool, cad_pool, covered, sink, rep, prog=(0.0, 1.0)):
    """도로 면 → 200m 조각별로 합침 → 지형 삼각형(25m 격자, 대각선 v00-v11)마다 잘라
    그 삼각형 평면 위에 놓는다. Blender 지형 메쉬와 같은 삼각형이므로 높이가 정확히 일치.
    지적도를 받은 칸(covered, 1.4km 격자)은 지적도 도로, 나머지는 OSM 도로."""
    from .vworld_api import BOX as CBOX
    cx, cy = terrain.origin
    res, z, E = terrain.res, terrain.z, terrain.exag
    x0, y0 = terrain.x0, terrain.y0              # 격자 (0,0) 로컬 좌표, 행은 아래(남)로
    H, W = z.shape
    allb0, kind0 = osm_pool
    tree0 = shapely.STRtree(allb0) if len(allb0) else None
    maj = allb0[kind0 == 0] if len(allb0) else allb0
    maj_tree = shapely.STRtree(maj) if len(maj) else None
    cad = None
    if cad_pool is not None:
        nonroad, water = cad_pool
        cad = (nonroad, water, shapely.STRtree(nonroad), shapely.STRtree(water) if len(water) else None)
    if tree0 is None and cad is None:
        return
    gx0, gx1 = x0 + cx, x0 + cx + (W - 1) * res          # 전역 범위 (격자 안쪽만)
    gy1, gy0 = y0 + cy, y0 + cy - (H - 1) * res
    ix0, ix1 = int(np.ceil(gx0 / CHUNK)), int(np.floor(gx1 / CHUNK))
    iy0, iy1 = int(np.ceil(gy0 / CHUNK)), int(np.floor(gy1 / CHUNK))
    nch = max(1, (ix1 - ix0) * (iy1 - iy0))
    m = int(round(CHUNK / res))
    done = 0
    for ix in range(ix0, ix1):
        for iy in range(iy0, iy1):
            done += 1
            if done % 200 == 0:
                rep.progress(prog[0] + (prog[1] - prog[0]) * done / nch)
            bx0, by0 = ix * CHUNK - cx, iy * CHUNK - cy          # 로컬
            gcx, gcy = (ix + 0.5) * CHUNK, (iy + 0.5) * CHUNK     # 전역 중심
            use_cad = cad is not None and (int(gcx // CBOX), int(gcy // CBOX)) in covered
            box = shapely.box(bx0, by0, bx0 + CHUNK, by0 + CHUNK)
            if sink.get("rd_major", bx0 + CHUNK / 2, by0 + CHUNK / 2) is None:
                continue                                         # 이 영역이 담당하지 않는 타일
            if use_cad:
                gmaj, gmin = _cad_roads(cad, box, maj_tree, maj)
            else:
                if tree0 is None:
                    continue
                idx = tree0.query(box, predicate="intersects")
                if not len(idx):
                    continue
                gmaj = shapely.union_all(allb0[idx[kind0[idx] == 0]]) if (kind0[idx] == 0).any() else None
                gmin = shapely.union_all(allb0[idx[kind0[idx] == 1]]) if (kind0[idx] == 1).any() else None
                if gmaj is not None:
                    gmaj = shapely.intersection(gmaj, box)
                if gmin is not None:
                    gmin = shapely.intersection(gmin, box)
                    if gmaj is not None:
                        gmin = shapely.difference(gmin, gmaj)
            # 이 조각의 삼각형들
            c0 = int(round((bx0 - x0) / res))
            r0 = int(round((y0 - (by0 + CHUNK)) / res))
            if c0 < 0 or r0 < 0 or c0 + m >= W or r0 + m >= H:
                continue
            rr, cc = np.meshgrid(np.arange(r0, r0 + m), np.arange(c0, c0 + m), indexing="ij")
            rr, cc = rr.ravel(), cc.ravel()
            X0 = x0 + cc * res
            X1 = X0 + res
            Yt = y0 - rr * res
            Yb = Yt - res
            z00, z01 = z[rr, cc] * E, z[rr, cc + 1] * E
            z10, z11 = z[rr + 1, cc] * E, z[rr + 1, cc + 1] * E
            # 삼각형 A: v00(X0,Yt) v10(X0,Yb) v11(X1,Yb) / B: v00 v11 v01(X1,Yt)
            triA = shapely.polygons(np.stack([np.c_[X0, Yt], np.c_[X0, Yb], np.c_[X1, Yb], np.c_[X0, Yt]], 1))
            triB = shapely.polygons(np.stack([np.c_[X0, Yt], np.c_[X1, Yb], np.c_[X1, Yt], np.c_[X0, Yt]], 1))
            # 평면 z = a*(x-X0) + b*(Yt-y) + c
            #  A: x 방향 기울기 (z11-z10)/res, 아래 방향 기울기 (z10-z00)/res
            #  B: x 방향 (z01-z00)/res, 아래 방향 (z11-z01)/res
            planes = [((z11 - z10) / res, (z10 - z00) / res), ((z01 - z00) / res, (z11 - z01) / res)]
            for kname, G in (("rd_major", gmaj), ("rd_minor", gmin)):
                if G is None or G.is_empty:
                    continue
                acc = sink.get(kname, bx0 + CHUNK / 2, by0 + CHUNK / 2)
                for ti, tris in enumerate((triA, triB)):
                    pieces = shapely.intersection(tris, G)
                    ok = np.where(~shapely.is_empty(pieces))[0]
                    a, b = planes[ti]
                    for j in ok:
                        _emit(acc, pieces[j], X0[j], Yt[j], a[j], b[j], z00[j])
    rep.progress(prog[1])


def _emit(acc, g, X0, Yt, a, b, c):
    gt = g.geom_type
    if gt == "Polygon":
        polys = [g]
    elif gt in ("MultiPolygon", "GeometryCollection"):
        polys = [p for p in getattr(g, "geoms", []) if p.geom_type == "Polygon"]
    else:
        return
    for pg in polys:
        if pg.area < 0.05:
            continue
        if len(pg.interiors):
            tr = shapely.constrained_delaunay_triangles(pg)
            parts = [list(t.exterior.coords)[:-1] for t in tr.geoms]
        else:
            parts = [list(pg.exterior.coords)[:-1]]
        for ring in parts:
            if len(ring) < 3:
                continue
            q = np.asarray(ring)
            if shapely.is_ccw(shapely.linearrings(np.vstack([q, q[:1]]))) is False:
                q = q[::-1]
            zz = a * (q[:, 0] - X0) + b * (Yt - q[:, 1]) + c + LIFT
            acc.add(np.c_[q, zz], [list(range(len(q)))])
