"""건물: 브이월드 GIS건물통합정보 + OSM 보완 → 압출 메쉬 (지형 과장 반영된 절대 높이)."""
import os
import numpy as np
import shapefile
import shapely
from shapely import wkb
from shapely.geometry import Polygon, Point
from .util import to_tm, ring_area, MeshAcc, Reporter


def gis_height(h, f, default):
    h = h or 0
    f = f or 0
    try:
        h = float(h)
        f = float(f)
    except Exception:
        return default, True
    if f > 0:
        fh = f * 3.3 + 1.5
        return (h if (h > 0 and f * 2 <= h <= fh * 1.8 + 10) else fh), False
    if 0 < h <= 40:
        return h, False
    return default, True


def build_buildings(s, osm, terrain, rep: Reporter, sink):
    """sink: store.TileSink — 건물 중심점이 속한 타일로 분배 (대상 타일 밖은 버림)"""
    cx, cy = terrain.origin
    exmin, eymin, exmax, eymax = terrain.extent
    gb = (exmin + cx, eymin + cy, exmax + cx, eymax + cy)   # 전역 TM 범위
    polys, hts, dflt = [], [], []

    # ---------- 1) 브이월드 GIS 건물 (라이브러리 인덱스) ----------
    from . import library
    rings = library.query(s.building_lib, gb, rep)
    for k, (rr, h0) in enumerate(rings):
        if k % 50000 == 0:
            rep.progress(0.45 * k / max(len(rings), 1))
        pg = Polygon(rr - [cx, cy])
        if not pg.is_valid:
            pg = pg.buffer(0)
        if pg.is_empty or pg.area < s.min_building_area:
            continue
        if pg.geom_type != "Polygon":
            pg = max(pg.geoms, key=lambda g: g.area)
        polys.append(pg)
        hts.append(h0 if h0 >= 0 else s.default_height)
        dflt.append(h0 < 0)
    # ---------- 1b) 브이월드 API 도로명주소건물 (층수 보완 + GIS 에 없는 건물) ----------
    if getattr(s, "api_heights", False):
        from . import vworld_api
        key = vworld_api.get_key()
        if key:
            import shapely as _sh
            api = vworld_api.fetch_spbd(key, gb, os.path.join(s.cache_dir, "spbd"), rep)
            tree0 = _sh.STRtree(polys) if polys else None
            filled = added = 0
            for fl, rr in api:
                pg = Polygon(rr - [cx, cy])
                if not pg.is_valid:
                    pg = pg.buffer(0)
                if pg.is_empty or pg.area < s.min_building_area:
                    continue
                if pg.geom_type != "Polygon":
                    pg = max(pg.geoms, key=lambda g: g.area)
                fh = fl * 3.3 + 1.5 if fl > 0 else None
                cover = 0.0
                if tree0 is not None:
                    idx = tree0.query(pg, predicate="intersects")
                    for k in idx:
                        ia = polys[k].intersection(pg).area
                        cover += ia
                        if fh and dflt[k] and ia / polys[k].area > 0.5:
                            hts[k] = fh
                            dflt[k] = False
                            filled += 1
                    cover /= pg.area
                if cover < 0.15:
                    polys.append(pg)
                    hts.append(fh or s.default_height)
                    dflt.append(fh is None)
                    added += 1
            rep.log(f"   API 층수로 높이 보완 {filled:,}동, GIS 에 없는 건물 추가 {added:,}동")
        else:
            rep.log("   (브이월드 인증키가 없어 API 높이 보완은 건너뜀)")
    hts = np.array(hts, float)
    dflt = np.array(dflt, bool)
    rep.log(f"   GIS 건물 {len(polys):,} (높이 정보 없음 {dflt.sum():,})")

    # ---------- 2) 랜드마크 ----------
    lms = []
    for lm in s.landmarks:
        X, Y = to_tm(lm["lon"], lm["lat"])
        if gb[0] < X < gb[2] and gb[1] < Y < gb[3]:
            lms.append(dict(lm, x=X - cx, y=Y - cy))

    # ---------- 3) OSM 보완 ----------
    added, addh = [], []
    if s.use_osm_buildings and osm["buildings"] and polys:
        tree = shapely.STRtree(polys)
        filled = 0
        nb = len(osm["buildings"])
        for bi, b in enumerate(osm["buildings"]):
            if bi % 20000 == 0:
                rep.progress(0.45 + 0.3 * bi / nb)
            g = wkb.loads(b["g"])
            g = shapely.transform(g, lambda c: np.c_[to_tm(c[:, 0], c[:, 1])] - [cx, cy])
            if not g.is_valid:
                g = g.buffer(0)
            if g.is_empty or g.area < max(20, s.min_building_area):
                continue
            oh = b["height"] if b["height"] and 2 < b["height"] < 600 else (
                b["levels"] * 3.3 + 1.5 if b["levels"] and 0 < b["levels"] < 130 else None)
            idx = tree.query(g, predicate="intersects")
            inter = np.array([polys[k].intersection(g).area for k in idx]) if len(idx) else np.array([])
            cover = inter.sum() / g.area if len(idx) else 0.0
            if oh:
                for k, ia in zip(idx, inter):
                    if dflt[k] and ia / polys[k].area > 0.5:
                        hts[k] = oh
                        dflt[k] = False
                        filled += 1
            if cover < 0.15:
                if any(Point(lm["x"], lm["y"]).distance(g) < 80 and (oh or 0) > lm["height"] * 0.5 for lm in lms):
                    continue
                for pg in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
                    if pg.area >= 20:
                        added.append(pg)
                        addh.append(oh or s.default_height)
        rep.log(f"   OSM 으로 높이 보완 {filled:,}동, 누락 건물 추가 {len(added):,}동")
    elif s.use_osm_buildings and osm["buildings"]:
        for b in osm["buildings"]:
            g = wkb.loads(b["g"])
            g = shapely.transform(g, lambda c: np.c_[to_tm(c[:, 0], c[:, 1])] - [cx, cy])
            oh = b["height"] or (b["levels"] * 3.3 + 1.5 if b["levels"] else s.default_height)
            for pg in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
                if pg.is_valid and pg.area >= 20:
                    added.append(pg)
                    addh.append(oh)

    allp = polys + added
    allh = np.r_[hts, np.array(addh, float)]

    # ---------- 4) 중복 제거 ----------
    best = {}
    for k, (pg, h) in enumerate(zip(allp, allh)):
        c = pg.centroid
        key = (round(c.x), round(c.y), round(pg.area / 5))
        if key not in best or allh[best[key]] < h:
            best[key] = k
    keep = sorted(best.values())
    rep.log(f"   중복 제거 {len(allp) - len(keep):,}건")
    allp = [allp[k] for k in keep]
    allh = allh[keep]

    # ---------- 5) 메쉬 (타일로 분배) ----------
    n = len(allp)
    cnt = 0
    for k, (pg, h) in enumerate(zip(allp, allh)):
        if k % 20000 == 0:
            rep.progress(0.75 + 0.22 * k / max(n, 1))
        c = pg.centroid
        acc = sink.get("bld", c.x, c.y)
        if acc is None:
            continue
        sp = pg.simplify(0.8, preserve_topology=True)
        if sp.is_empty or sp.geom_type != "Polygon":
            continue
        ring = np.asarray(sp.exterior.coords)[:-1, :2]
        if len(ring) < 3:
            continue
        if ring_area(ring) < 0:
            ring = ring[::-1]
        g = terrain.ground(ring[:, 0], ring[:, 1])
        gc = float(terrain.ground([ring[:, 0].mean()], [ring[:, 1].mean()])[0])
        acc.add_prism(ring, min(g.min(), gc) - 4, max(g.max(), gc) + h)
        cnt += 1
    # ---------- 6) 랜드마크 ----------
    nl = 0
    for lm in lms:
        acc = sink.get("bld", lm["x"], lm["y"])
        if acc is not None:
            _taper(acc, terrain, lm)
            nl += 1
    rep.log(f"   건물 {cnt:,}동, 랜드마크 {nl}개")
    rep.progress(1.0)


def _taper(acc, terrain, lm, seg=24):
    x, y = lm["x"], lm["y"]
    b = float(terrain.ground([x], [y])[0])
    h = lm["height"]
    r0, r1, sq = lm.get("r0", 20), lm.get("r1", 8), lm.get("square", 0.0)
    ang = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    rr = 1 / np.maximum(np.abs(np.cos(ang)), np.abs(np.sin(ang))) ** sq
    tiers = np.linspace(0, 1, 9)
    V = []
    for k, tt in enumerate(tiers):
        r = r0 + (r1 - r0) * tt ** 1.3
        V.append(np.c_[x + r * rr * np.cos(ang), y + r * rr * np.sin(ang), np.full(seg, b - 4 if k == 0 else b + h * tt)])
    polys = []
    i = np.arange(seg)
    j = (i + 1) % seg
    for k in range(len(tiers) - 1):
        a0, a1 = k * seg, (k + 1) * seg
        polys += [[a0 + p, a0 + q, a1 + q, a1 + p] for p, q in zip(i, j)]
    polys.append(list((len(tiers) - 1) * seg + i))
    acc.add(np.vstack(V), polys)
