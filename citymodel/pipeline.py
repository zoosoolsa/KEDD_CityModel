"""작업 흐름: 카메라(또는 범위) → 필요한 타일 → (있으면 카메라만 추가 | 없거나 오래되면 생성·재조립)."""
import json
import os
import time
import numpy as np
from .config import Settings
from .util import Reporter, to_tm
from . import blender_runner, library, osmdata
from .store import Store, Project, build_tiles, resolve_camera, camera_tiles, tiles_for_bbox_ll, tile_bounds, TILE


def _gis_meta(s, rep):
    """{code: (date, bounds)} — 인덱스가 없으면 만든다"""
    if not os.path.isdir(s.building_lib):
        return {}
    sc = library.ensure_indexes(s.building_lib, rep)
    out = {}
    for c, e in sc.items():
        m = library.read_meta(s.building_lib, c, e["date"])
        if m:
            out[c] = (e["date"], m["bounds"])
    return out


def plan(s: Settings, project_name, cams, rep: Reporter = None):
    """미리보기용 계획: 필요한 타일, 새로/다시 만들 타일, 카메라만 추가 가능 여부"""
    rep = rep or Reporter(log=lambda m: None)
    store = Store(s.work_dir, exag=s.exag, res=s.dem_res)
    needed = set()
    for c in cams:
        needed |= camera_tiles(c, store)
    gis = _gis_meta(s, rep)
    pbf_ver = _osm_version(s)
    from . import vworld_api
    api_on = bool(s.api_heights and vworld_api.get_key())
    proj = Project(store, project_name)
    allk = needed | {tuple(k) for k in proj.d["tiles"]}
    wanted = {k: store.wanted_sources(k, gis, pbf_ver, extra=dict(api=api_on)) for k in allk}
    states = {k: (store.tile_state(k, wanted[k]) if pbf_ver else ("ok" if store.tile_meta(k) else "missing")) for k in allk}
    build = sorted(k for k, st in states.items() if st != "ok")
    cam_only = not build and proj.is_current(needed)
    needed = allk if build else needed
    return dict(store=store, project=proj, needed=needed, build=build, states=states, wanted=wanted,
                camera_only=cam_only, gis=gis, exag_changed=abs(store.exag - s.exag) > 1e-6)


def _osm_version(s):
    import datetime as dt
    pbf = s.osm_pbf or os.path.join(s.cache_dir, "osm", "south-korea-latest.osm.pbf")
    return dt.date.fromtimestamp(os.path.getmtime(pbf)).isoformat() if os.path.exists(pbf) else None


def scene_cfg(s: Settings, store: Store, proj: Project):
    tiles = sorted(proj.tiles)
    if proj.d.get("origin") is None:
        xs = [k[0] for k in tiles]
        ys = [k[1] for k in tiles]
        proj.d["origin"] = [(min(xs) + max(xs) + 1) / 2 * TILE, (min(ys) + max(ys) + 1) / 2 * TILE]
    origin = proj.d["origin"]
    cams = [resolve_camera(store, c, origin) for c in proj.d["cameras"]]
    return dict(
        exag=store.exag, origin=origin,
        tiles=[dict(dir=store.tile_dir(k), x0=k[0] * TILE, y0=k[1] * TILE) for k in tiles],
        cameras=cams, minor_roads=s.include_minor_roads,
        col_building=list(s.col_building), col_ground=list(s.col_ground), col_green=list(s.col_green),
        col_water=list(s.col_water), col_road=list(s.col_road),
        ao_distance=s.ao_distance, ao_strength=s.ao_strength,
        render_w=s.render_w, render_h=s.render_h, samples=s.samples,
        thumbs=_thumbs_dir(proj, s), project=proj.path,
    )


def _thumbs_dir(proj, s):
    """카메라 썸네일 폴더 (끄면 None)"""
    return os.path.join(proj.dir, "thumbs") if s.thumbnails else None


def _name_cams(proj, cams):
    import uuid
    import datetime as dt
    n = len(proj.d["cameras"])
    now = dt.datetime.now().isoformat(timespec="minutes")
    for i, c in enumerate(cams):
        src = {"vworld": "브이월드", "view": c.get("source", "웹지도"), "default": "범위"}.get(c["kind"], "cam")
        c.setdefault("id", uuid.uuid4().hex[:8])
        c.setdefault("created", now)
        if not c.get("name"):
            c["name"] = f"Cam_{n + i + 1:02d}_{src}"
        if not c.get("label"):
            if c["kind"] == "vworld":
                c["label"] = f"{src} · {c['lat']:.5f}, {c['lon']:.5f} · 높이 {c['height']:.0f}m · 방위 {c['heading']:.0f}°"
            elif c["kind"] == "view":
                c["label"] = f"{src} · {c['lat']:.5f}, {c['lon']:.5f} · 거리 {c['range']:.0f}m"
            else:
                c["label"] = src
    return cams


def _ensure_ids(proj):
    """예전 프로젝트의 카메라에 id 부여"""
    import uuid
    for c in proj.d["cameras"]:
        c.setdefault("id", uuid.uuid4().hex[:8])


def run_job(s: Settings, project_name, cams, rep: Reporter, cancel=lambda: False):
    """cams: 카메라 사양 목록 (kind=vworld/view/default). 반환: (blend 경로, 수행 내용)"""
    t0 = time.time()
    os.makedirs(s.cache_dir, exist_ok=True)
    rep.set_stage(0.0, 0.03, "준비")
    store = Store(s.work_dir, exag=s.exag, res=s.dem_res)
    if store.set_exag(s.exag):
        rep.log(f"⚠ 지형 과장이 {s.exag} 로 바뀌어 기존 타일을 모두 다시 만듭니다")
    blender = "" if s.blender_path == "__none__" else (s.blender_path or blender_runner.find_blender())
    proj = Project(store, project_name)
    _ensure_ids(proj)
    cams = _name_cams(proj, [dict(c) for c in cams])

    needed = set()
    for c in cams:
        needed |= camera_tiles(c, store)
    if not needed:
        raise RuntimeError("카메라가 보는 범위를 계산하지 못했습니다")
    gis = _gis_meta(s, rep)
    miss = [r["name"] for r in library.overview(s.building_lib, _ll_bounds(needed))[0] if r["need"] and r["state"] == "없음"]
    if miss:
        rep.log(f"   ⚠ 이 범위의 건물 데이터가 없는 시도: {', '.join(miss)} (그 부분은 OSM 건물만 들어갑니다)")
    _, osm_ver = osmdata.ensure_pbf(s, rep, max_age_days=s.osm_max_age_days)
    from . import vworld_api
    api_on = bool(s.api_heights and vworld_api.get_key())
    sources_for = lambda k: store.wanted_sources(k, gis, osm_ver, extra=dict(api=api_on))
    # 이 프로젝트에 이미 들어 있는 타일도 예전 방식이면 함께 다시 만듦 (섞이면 경계가 어긋남)
    have = {tuple(k) for k in proj.d["tiles"]}
    build = sorted(k for k in (needed | have) if store.tile_state(k, sources_for(k)) != "ok")
    rep.log(f"   필요한 타일 {len(needed)}개 (4km) · 새로/다시 만들 타일 {len(build)}개")

    if not build and proj.is_current(needed):
        # ---- 카메라만 추가 ----
        if not blender:
            raise RuntimeError("Blender 를 찾지 못했습니다")
        rep.set_stage(0.1, 1.0, "카메라만 추가 (모델 재사용)")
        resolved = [resolve_camera(store, c, proj.d["origin"]) for c in cams]
        cj = os.path.join(proj.dir, "cameras_add.json")
        json.dump(dict(cameras=resolved, cfg=dict(render_w=s.render_w, thumbs=_thumbs_dir(proj, s), project=proj.path,
                                                  origin=proj.d["origin"])),
                  open(cj, "w", encoding="utf-8"), ensure_ascii=False)
        blender_runner.run_cameras(blender, proj.blend, cj, rep, cancel)
        proj.d["cameras"] += cams
        proj.save()
        blender_runner.install_addon(blender, rep.log)
        rep.log(f"✔ 카메라 {len(cams)}개 추가 ({time.time() - t0:.0f}초) → {proj.blend}")
        return proj.blend, "camera"

    if build:
        rep.stage_root = (0.03, 0.80)
        build_tiles(store, build, s, rep, sources_for, cancel)
    # ---- 장면 재조립 (기존 타일·카메라 유지 + 새 범위) ----
    proj.d["tiles"] = sorted({tuple(t) for t in proj.d["tiles"]} | needed)
    proj.d["cameras"] += cams
    cfg = scene_cfg(s, store, proj)
    os.makedirs(proj.dir, exist_ok=True)
    sj = os.path.join(proj.dir, "scene.json")
    json.dump(cfg, open(sj, "w", encoding="utf-8"), ensure_ascii=False)
    if not blender:
        rep.log("⚠ Blender 를 찾지 못해 .blend 조립은 건너뜁니다")
        proj.save()
        return None, "tiles"
    rep.set_stage(0.80, 1.0, f"Blender 장면 조립 (타일 {len(proj.d['tiles'])}개, 카메라 {len(proj.d['cameras'])}개)")
    tmp = proj.blend + ".new.blend"
    blender_runner.run_scene(blender, sj, tmp, rep, preview=None, cancel=cancel, ntiles=len(proj.d["tiles"]))
    os.replace(tmp, proj.blend)
    proj.d["versions"] = {f"{k[0]}_{k[1]}": (store.tile_meta(k) or {}).get("built") for k in proj.tiles}
    proj.save()
    blender_runner.install_addon(blender, rep.log)
    rep.log(f"✔ 완료 ({(time.time() - t0) / 60:.1f}분) → {proj.blend}")
    return proj.blend, "build"


def _ll_bounds(keys):
    from .util import to_ll
    xs = [k[0] for k in keys]
    ys = [k[1] for k in keys]
    lo, la = to_ll(np.array([min(xs) * TILE, (max(xs) + 1) * TILE]), np.array([min(ys) * TILE, (max(ys) + 1) * TILE]))
    return (min(lo), min(la), max(lo), max(la))


def bbox_camera(s: Settings):
    """범위 입력 모드: 범위를 덮는 기본 카메라"""
    lon0, lat0, lon1, lat1 = s.bbox
    xs, ys = to_tm(np.array([lon0, lon1, lon0, lon1]), np.array([lat0, lat0, lat1, lat1]))
    return dict(kind="default", tm_bounds=[float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))])


def url_camera(s: Settings, url):
    from .webcam import parse_url
    v = parse_url(url)
    return dict(kind="view", lat=v["lat"], lon=v["lon"], range=v["range"], heading=v["heading"], tilt=v["tilt"],
                fov=v["fov"], source=v["source"], aspect=1.5)
