"""도로는 지면 텍스처(landcover.py)에 칠하고, 여기서는 다리(조각 병합, 양끝 도로 높이에 맞춘 경사 상판, 교각)."""
import os
import numpy as np
import shapely
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge
from scipy import ndimage as nd
from .util import to_tm, MeshAcc, densify, offset_sides, Reporter

WID = {'motorway': 14, 'trunk': 13, 'primary': 14, 'secondary': 11, 'tertiary': 9, 'motorway_link': 8,
       'trunk_link': 8, 'primary_link': 8, 'secondary_link': 7, 'tertiary_link': 6, 'residential': 6,
       'unclassified': 6, 'living_street': 5, 'rail': 10}
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
    off = s.road_offset
    blines, bw = [], []
    ways = osm["ways"]
    for k, w in enumerate(ways):
        if k % 5000 == 0:
            rep.progress(0.3 * k / max(len(ways), 1))
        if not w['bridge']:
            continue                 # 일반 도로는 지면 텍스처(landcover.py)에 칠함
        lon, lat = np.array(w['pts']).T
        X, Y = to_tm(lon, lat)
        p = np.c_[X - cx, Y - cy]
        if len(p) >= 2:
            blines.append(LineString(p))
            bw.append(_width(w))

    # ---- 다리 ----
    if blines:
        merged = linemerge(MultiLineString(blines))
        merged = list(merged.geoms) if merged.geom_type == "MultiLineString" else [merged]
        tree = shapely.STRtree(blines)
        rep.log(f"   다리 구간 {len(blines):,} → 병합 {len(merged):,}")
        for mi, ml in enumerate(merged):
            if mi % 500 == 0:
                rep.progress(0.6 + 0.38 * mi / len(merged))
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
