"""지형: Copernicus DEM 30m 다운로드 → 병합 → EPSG:5186 격자로 재투영 → 건물/수목 제거 → 물 영역 추출."""
import math
import os
import numpy as np
import tifffile
from scipy import ndimage as nd
from .util import download, to_tm, to_ll, Reporter

COP_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
           "Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM/"
           "Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM.tif")


def _read_geotiff(path):
    """EPSG:4326 GeoTIFF → (array, lon0, lat0, dlon, dlat) — lon0/lat0 는 (0,0) 픽셀 중심."""
    with tifffile.TiffFile(path) as t:
        a = t.pages[0].asarray().astype(np.float32)
        g = t.geotiff_metadata or {}
    sx, sy = g["ModelPixelScale"][:2]
    tp = g["ModelTiepoint"]
    lon0, lat0 = tp[3] - tp[0] * sx, tp[4] + tp[1] * sy
    rt = g.get("GTRasterTypeGeoKey", 1)
    try:
        rt = int(rt)
    except Exception:
        rt = 2 if "Point" in str(rt) else 1
    if rt == 1:  # PixelIsArea → 픽셀 중심으로 보정
        lon0 += sx / 2
        lat0 -= sy / 2
    return a, lon0, lat0, sx, sy


def build_terrain(s, rep: Reporter, out_path):
    lon_min, lat_min, lon_max, lat_max = s.bbox
    res = s.dem_res
    # --- 타일 확보 ---
    tiles = []
    if s.dem_files:
        tiles = list(s.dem_files)
    else:
        need = [(la, lo) for la in range(math.floor(lat_min), math.floor(lat_max) + 1)
                for lo in range(math.floor(lon_min), math.floor(lon_max) + 1)]
        for i, (la, lo) in enumerate(need):
            dest = os.path.join(s.cache_dir, "dem", f"cop30_N{la}_E{lo}.tif")
            rep.log(f"   지형 타일 N{la} E{lo} 확인/다운로드")
            p = download(COP_URL.format(lat=la, lon=lo), dest, rep, label=f"DEM N{la}E{lo}")
            if p:
                tiles.append(p)
            else:
                rep.log(f"   (N{la} E{lo} 타일 없음 → 바다로 간주)")
            rep.progress((i + 1) / len(need) * 0.5)
    if not tiles:
        raise RuntimeError("지형(DEM) 타일을 하나도 얻지 못했습니다.")

    # --- 목표 격자 (EPSG:5186) ---
    xs, ys = to_tm(np.array([lon_min, lon_max, lon_min, lon_max]), np.array([lat_min, lat_min, lat_max, lat_max]))
    xmin, xmax = np.floor((min(xs) - 1000) / res) * res, np.ceil((max(xs) + 1000) / res) * res
    ymin, ymax = np.floor((min(ys) - 1000) / res) * res, np.ceil((max(ys) + 1000) / res) * res
    W, H = int((xmax - xmin) / res), int((ymax - ymin) / res)
    cx, cy = round((xmin + xmax) / 2 / 10) * 10, round((ymin + ymax) / 2 / 10) * 10
    rep.log(f"   지형 격자 {W}×{H} ({W*res/1000:.1f}×{H*res/1000:.1f}km, {res:.0f}m)")

    X = xmin + (np.arange(W) + 0.5) * res
    Y = ymax - (np.arange(H) + 0.5) * res
    out = np.full((H, W), np.nan, np.float32)
    for ti, path in enumerate(tiles):
        a, lon0, lat0, dlon, dlat = _read_geotiff(path)
        # 행 단위로 처리 (메모리 절약)
        for r0 in range(0, H, 256):
            r1 = min(H, r0 + 256)
            gx, gy = np.meshgrid(X, Y[r0:r1])
            lon, lat = to_ll(gx, gy)
            col = (lon - lon0) / dlon
            row = (lat0 - lat) / dlat
            inside = (col >= -0.5) & (col <= a.shape[1] - 0.5) & (row >= -0.5) & (row <= a.shape[0] - 0.5)
            if not inside.any():
                continue
            v = nd.map_coordinates(a, [np.clip(row, 0, a.shape[0] - 1), np.clip(col, 0, a.shape[1] - 1)], order=1, mode="nearest")
            blk = out[r0:r1]
            blk[inside] = v[inside]
        rep.progress(0.5 + 0.3 * (ti + 1) / len(tiles))
    out = np.nan_to_num(out, nan=0.0)
    out[out < -100] = 0.0

    # --- 물 (평탄 + 저지대 + 큰 덩어리) ---
    mx = nd.maximum_filter(out, 3)
    mn = nd.minimum_filter(out, 3)
    flat = (mx - mn < 0.05) & (out < 20)
    lab, n = nd.label(flat)
    if n:
        sz = nd.sum(flat, lab, range(1, n + 1))
        water = np.isin(lab, 1 + np.where(sz > 300 * (30.0 / res) ** 2)[0])
    else:
        water = np.zeros_like(flat)
    water = nd.binary_closing(water, iterations=4)
    water = nd.binary_fill_holes(water)
    water = nd.binary_opening(water, iterations=1)
    rep.progress(0.9)

    # --- 지면 (DSM 의 건물·수목 덩어리 제거) ---
    k = int(round(210 / res)) | 1
    ground = nd.grey_opening(out, size=(k, k))
    ground = nd.gaussian_filter(ground, 1.0)
    wl = float(np.median(out[water])) if water.any() else 0.0
    ground[water] = wl - 2.0

    np.savez_compressed(out_path, z=ground.astype(np.float32), water=water.astype(np.uint8), res=res,
                        x0=xmin - cx, y0=ymax - cy, water_level=wl, origin=np.array([cx, cy]))
    rep.progress(1.0)
    rep.log(f"   고도 {ground.min():.0f}~{ground.max():.0f}m, 물 {water.mean()*100:.1f}%")
    return out_path


def hillshade_png(terrain_npz, path, width=1000):
    from PIL import Image
    t = np.load(terrain_npz)
    z = t["z"].astype(float)
    res = float(t["res"])
    gy, gx = np.gradient(z, res)
    sl = np.arctan(np.hypot(gx, gy))
    asp = np.arctan2(-gx, gy)
    az, alt = np.radians(315), np.radians(45)
    hs = np.sin(alt) * np.cos(sl) + np.cos(alt) * np.sin(sl) * np.cos(az - asp)
    im = Image.fromarray((np.clip(hs, 0, 1) * 255).astype("uint8"))
    im = im.resize((width, int(width * im.height / im.width)))
    im.save(path)
    return path
