"""전체 처리 순서."""
import json
import math
import os
import time
from .config import Settings
from .util import Reporter, Terrain
from . import dem, osmdata, landcover, buildings, roads, blender_runner, library


def camera_json(s: Settings, terrain: Terrain, rep: Reporter = None):
    """카메라: 웹 지도 URL 이 있으면 그 시점, 없으면 범위에 맞춘 기본 구도"""
    if s.camera_url.strip():
        from .webcam import parse_url, camera_from_view
        v = parse_url(s.camera_url)
        cam = camera_from_view(v, terrain)
        if rep:
            rep.log(f"   카메라: {v['source']} 시점 (거리 {v['range']:.0f}m, 방위 {v['heading']:.0f}°, 기울기 {v['tilt']:.0f}°)")
        return cam
    xmin, ymin, xmax, ymax = terrain.extent
    W, H = xmax - xmin, ymax - ymin
    M = max(W, H)
    tgt = [(xmin + xmax) / 2, (ymin + ymax) / 2 - 0.06 * H, 0.0]
    loc = [tgt[0], tgt[1] - 0.31 * M, 0.17 * M]
    t = math.atan2(0.31 * M, 0.17 * M)
    fov = math.degrees(2 * math.atan(24 / 2 / 30))   # 30mm 렌즈 (가로 36mm 기준 세로 화각)
    return dict(location=loc, rotation=[t, 0.0, 0.0], fov=fov, source="기본")


def scene_json(s: Settings, terrain: Terrain, path, rep=None):
    cfg = dict(
        exag=s.exag,
        col_building=list(s.col_building), col_ground=list(s.col_ground), col_green=list(s.col_green),
        col_water=list(s.col_water), col_road=list(s.col_road),
        ao_distance=s.ao_distance, ao_strength=s.ao_strength,
        render_w=s.render_w, render_h=s.render_h, samples=s.samples,
        camera=camera_json(s, terrain, rep),
    )
    json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def update_camera(s: Settings, blend_path, rep: Reporter):
    """이미 만든 .blend 의 카메라만 웹 지도 시점으로 교체 (새 카메라 추가, 기존 것 유지)"""
    d = os.path.join(s.project_dir, "data")
    terrain = Terrain(os.path.join(d, "terrain.npz"), exag=s.exag)
    cam = camera_json(s, terrain, rep)
    cp = os.path.join(d, "camera_update.json")
    json.dump(cam, open(cp, "w", encoding="utf-8"), ensure_ascii=False)
    blender = s.blender_path or blender_runner.find_blender()
    if not blender:
        raise RuntimeError("Blender 를 찾지 못했습니다")
    blender_runner.run_camera_update(blender, d, blend_path, cp, rep)


def run(s: Settings, rep: Reporter, cancel=lambda: False):
    """반환: (blend 경로, 미리보기 png 목록)"""
    t0 = time.time()
    d = os.path.join(s.project_dir, "data")
    os.makedirs(d, exist_ok=True)
    os.makedirs(s.cache_dir, exist_ok=True)
    s.save(os.path.join(s.project_dir, "settings.json"))
    previews = []

    blender = "" if s.blender_path == "__none__" else (s.blender_path or blender_runner.find_blender())
    if not blender:
        rep.log("⚠ Blender 를 찾지 못했습니다. 데이터까지만 만들고 .blend 생성은 건너뜁니다.")

    rep.set_stage(0.00, 0.15, "1/6 지형 (Copernicus DEM 30m)")
    tpath = os.path.join(d, "terrain.npz")
    dem.build_terrain(s, rep, tpath)
    terrain = Terrain(tpath, exag=s.exag)
    previews.append(dem.hillshade_png(tpath, os.path.join(s.project_dir, "preview_terrain.png")))

    rep.set_stage(0.15, 0.35, "2/6 OpenStreetMap (도로·건물·녹지·물)")
    osm = osmdata.get_osm(s, rep)

    rep.set_stage(0.35, 0.45, "3/6 녹지·물 텍스처")
    lc = landcover.build_landcover(s, osm, terrain, rep, os.path.join(d, "landcover.png"))
    previews.append(landcover.preview_png(lc, os.path.join(s.project_dir, "preview_landcover.png"), s))

    rep.set_stage(0.45, 0.72, "4/6 건물 (GIS건물통합정보 + OSM 보완)")
    if s.building_zips:
        got = library.import_files(s.building_lib, s.building_zips)
        if got:
            rep.log(f"   건물 데이터 폴더로 {len(got)}개 가져옴")
    buildings.build_buildings(s, osm, terrain, rep, d)

    rep.set_stage(0.72, 0.82, "5/6 도로·다리")
    roads.build_roads(s, osm, terrain, rep, d)

    scene_json(s, terrain, os.path.join(d, "scene.json"), rep)
    out = os.path.join(s.project_dir, f"{s.project_name}.blend")
    if blender:
        rep.set_stage(0.82, 1.00, "6/6 Blender 장면 생성")
        pv = os.path.join(s.project_dir, "preview_render.png") if s.preview_render else None
        blender_runner.run_blender(blender, d, out, rep, preview=pv, cancel=cancel)
        if pv and os.path.exists(pv):
            previews.append(pv)
    else:
        out = None
    rep.progress(1.0)
    rep.log(f"✔ 완료 ({(time.time() - t0) / 60:.1f}분) → {out or d}")
    return out, previews
