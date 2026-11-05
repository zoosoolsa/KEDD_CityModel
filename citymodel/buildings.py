"""건물: 브이월드 GIS건물통합정보 + OSM 보완 → 압출 메쉬 (지형 과장 반영된 절대 높이)."""
import glob
import os
import zipfile
import numpy as np
import shapefile
import shapely
from shapely import wkb
from shapely.geometry import Polygon, Point
from pyproj import CRS, Transformer
from .util import to_tm, ring_area, MeshAcc, Reporter


def _extract_shps(paths, cache_dir, rep):
    """zip 이면 풀어서 .shp 경로 목록 반환"""
    shps = []
    for p in paths:
        if p.lower().endswith(".shp"):
            shps.append(p)
            continue
        dest = os.path.join(cache_dir, "gis", os.path.splitext(os.path.basename(p))[0])
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if not n.startswith("__MACOSX") and not os.path.basename(n).startswith("._")]
            todo = [n for n in names if not os.path.exists(os.path.join(dest, n))]
            if todo:
                rep.log(f"   압축 해제: {os.path.basename(p)}")
                for n in todo:
                    z.extract(n, dest)
        shps += [f for f in glob.glob(os.path.join(dest, "**", "*.shp"), recursive=True)]
    return shps


def _encoding(shp):
    cst = os.path.splitext(shp)[0] + ".cst"
    if os.path.exists(cst):
        e = open(cst, encoding="ascii", errors="ignore").read().strip().lower()
        if e in ("euc-kr", "cp949", "ks_c_5601-1987"):
            return "cp949"
        if e:
            return e
    return "cp949"


def _transformer(shp):
    prj = os.path.splitext(shp)[0] + ".prj"
    if os.path.exists(prj):
        try:
            c = CRS.from_wkt(open(prj, encoding="utf-8", errors="ignore").read())
            if c.to_epsg() == 5186:
                return None
            return Transformer.from_crs(c, 5186, always_xy=True)
        except Exception:
            pass
    return None


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


def build_buildings(s, osm, terrain, rep: Reporter, out_dir):
    cx, cy = terrain.origin
    exmin, eymin, exmax, eymax = terrain.extent
    gb = (exmin + cx, eymin + cy, exmax + cx, eymax + cy)   # 전역 TM 범위
    polys, hts, dflt = [], [], []

    # ---------- 1) 브이월드 GIS 건물 ----------
    shps = _extract_shps(s.building_zips, s.cache_dir, rep)
    if not shps:
        raise RuntimeError("건물 파일(.shp)을 찾지 못했습니다.")
    for si, shp in enumerate(shps):
        enc = _encoding(shp)
        tr = _transformer(shp)
        r = shapefile.Reader(shp, encoding=enc, encodingErrors="replace")
        names = [f[0] for f in r.fields[1:]]
        ih = names.index("A16") if "A16" in names else None
        iff = names.index("A26") if "A26" in names else None
        if ih is None and iff is None:
            rep.log(f"   ⚠ {os.path.basename(shp)}: 높이/층수 필드(A16/A26)가 없어 기본 높이를 씁니다")
        N = len(r)
        rep.log(f"   {os.path.basename(shp)}: {N:,}동 검사")
        inb = 0
        for i, sh in enumerate(r.iterShapes()):
            if i % 50000 == 0:
                rep.progress(0.45 * (si + i / max(N, 1)) / len(shps))
            if not sh.points:
                continue
            bx = sh.bbox
            if tr is None and (bx[2] < gb[0] or bx[0] > gb[2] or bx[3] < gb[1] or bx[1] > gb[3]):
                continue
            pts = np.asarray(sh.points, float)
            if tr is not None:
                pts = np.c_[tr.transform(pts[:, 0], pts[:, 1])]
                if pts[:, 0].max() < gb[0] or pts[:, 0].min() > gb[2] or pts[:, 1].max() < gb[1] or pts[:, 1].min() > gb[3]:
                    continue
            rec = r.record(i)
            hgt, isd = gis_height(rec[ih] if ih is not None else 0, rec[iff] if iff is not None else 0, s.default_height)
            parts = list(sh.parts) + [len(pts)]
            for p in range(len(parts) - 1):
                rr = pts[parts[p]:parts[p + 1]]
                if len(rr) < 4 or ring_area(rr) > 0:      # shapefile 은 외곽=시계방향, 구멍=반시계
                    continue
                pg = Polygon(rr - [cx, cy])
                if not pg.is_valid:
                    pg = pg.buffer(0)
                if pg.is_empty or pg.area < s.min_building_area:
                    continue
                if pg.geom_type != "Polygon":
                    pg = max(pg.geoms, key=lambda g: g.area)
                polys.append(pg)
                hts.append(hgt)
                dflt.append(isd)
                inb += 1
        rep.log(f"   → 범위 안 {inb:,}동")
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

    # ---------- 5) 메쉬 (타일 분할) ----------
    NT = 4 if len(allp) > 200000 else (2 if len(allp) > 30000 else 1)
    tiles = [MeshAcc() for _ in range(NT * NT)]
    n = len(allp)
    for k, (pg, h) in enumerate(zip(allp, allh)):
        if k % 20000 == 0:
            rep.progress(0.75 + 0.22 * k / max(n, 1))
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
        top = max(g.max(), gc) + h
        bot = min(g.min(), gc) - 4
        gx, gy = ring.mean(0)
        ti = min(max(int((gx - exmin) / (exmax - exmin) * NT), 0), NT - 1) + \
            NT * min(max(int((gy - eymin) / (eymax - eymin) * NT), 0), NT - 1)
        tiles[ti].add_prism(ring, bot, top)
    for f in glob.glob(os.path.join(out_dir, "bld_*.npz")):
        os.remove(f)
    tot = 0
    for ti, acc in enumerate(tiles):
        tot += acc.save(os.path.join(out_dir, f"bld_{ti:02d}.npz"))

    # ---------- 6) 랜드마크 메쉬 ----------
    acc = MeshAcc()
    for lm in lms:
        _taper(acc, terrain, lm)
    acc.save(os.path.join(out_dir, "bld_landmarks.npz"))
    rep.log(f"   건물 {len(allp):,}동 (꼭짓점 {tot:,}), 랜드마크 {len(lms)}개")
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
