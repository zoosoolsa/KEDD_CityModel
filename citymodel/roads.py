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

    rep.log(f"   도로 {len(rmaj):,} (주요) · {len(rmin):,} (이면) → 지형에 얹기")
    drape_roads(terrain, rmaj, rmin, sink, rep, (0.3, 0.75))

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
def drape_roads(terrain, rmaj, rmin, sink, rep, prog=(0.0, 1.0)):
    """도로 폭으로 버퍼 → 200m 조각별로 합침 → 지형 삼각형(25m 격자, 대각선 v00-v11)마다 잘라
    그 삼각형 평면 위에 놓는다. Blender 지형 메쉬와 같은 삼각형이므로 높이가 정확히 일치."""
    cx, cy = terrain.origin
    res, z, E = terrain.res, terrain.z, terrain.exag
    x0, y0 = terrain.x0, terrain.y0              # 격자 (0,0) 로컬 좌표, 행은 아래(남)로
    H, W = z.shape

    def buffers(items):
        if not items:
            return np.array([], dtype=object)
        lines = np.array([l for l, _ in items], dtype=object)
        wd = np.array([w for _, w in items]) / 2.0
        g = shapely.buffer(lines, wd, quad_segs=3, cap_style="round", join_style="round")
        return shapely.simplify(g, 0.2)

    bmaj, bmin = buffers(rmaj), buffers(rmin)
    allb = np.concatenate([bmaj, bmin]) if len(bmaj) + len(bmin) else np.array([], dtype=object)
    if not len(allb):
        return
    kind = np.r_[np.zeros(len(bmaj), int), np.ones(len(bmin), int)]
    tree = shapely.STRtree(allb)
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
            box = shapely.box(bx0, by0, bx0 + CHUNK, by0 + CHUNK)
            idx = tree.query(box, predicate="intersects")
            if not len(idx):
                continue
            if sink.get("rd_major", bx0 + CHUNK / 2, by0 + CHUNK / 2) is None:
                continue                                         # 이 영역이 담당하지 않는 타일
            gmaj = shapely.union_all(allb[idx[kind[idx] == 0]]) if (kind[idx] == 0).any() else None
            gmin = shapely.union_all(allb[idx[kind[idx] == 1]]) if (kind[idx] == 1).any() else None
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
