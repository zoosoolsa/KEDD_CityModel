bl_info = {
    "name": "KEDD CityModel 카메라",
    "author": "KEDD",
    "version": (1, 2, 0),
    "blender": (3, 3, 0),
    "location": "3D 뷰 > 사이드바(N) > KEDD",
    "description": "KEDD CityModel 카메라 히스토리 — 썸네일을 눌러 활성 카메라·뷰 전환",
    "category": "Camera",
}
# Blender 3.3 ~ 5.x. 앱이 애드온으로 설치하고, 앱에서 열 때는 스크립트로도 실행된다.
#   blender 파일.blend --python kedd_cameras.py -- --kedd-camera <카메라 이름>
import json
import os
import sys
import uuid
import datetime as dt
import bpy
from bpy.props import IntProperty

COLL = "Cameras"


# ------------------------------------------------------------------ 공통
def cams(sc=None):
    c = bpy.data.collections.get(COLL)
    obs = [o for o in (c.all_objects if c else bpy.data.objects) if o.type == "CAMERA"]
    return sorted(obs, key=lambda o: (o.get("kedd_created", ""), o.data.name))


def thumb_of(ob):
    im = bpy.data.images.get(ob.get("kedd_thumb", "") or "")
    if im:
        try:
            im.preview_ensure()
        except Exception:
            pass
    return im


def icon_of(ob):
    im = thumb_of(ob)
    try:
        return im.preview.icon_id if im and im.preview else 0
    except Exception:
        return 0


def activate(context, ob):
    sc = context.scene
    sc.camera = ob
    r = ob.get("kedd_res")
    if r and len(r) == 2:
        sc.render.resolution_x, sc.render.resolution_y = int(r[0]), int(r[1])
    for win in context.window_manager.windows:
        for ar in win.screen.areas:
            if ar.type == "VIEW_3D":
                for sp in ar.spaces:
                    if sp.type == "VIEW_3D":
                        if sp.region_3d.view_perspective != "CAMERA":
                            sp.region_3d.view_perspective = "CAMERA"
                        try:
                            sp.region_3d.view_camera_offset = (0.0, 0.0)
                            sp.region_3d.view_camera_zoom = 0.0
                        except Exception:
                            pass
                ar.tag_redraw()


def _on_index(self, context):
    lst = cams()
    i = self.kedd_cam_index
    if 0 <= i < len(lst) and context.scene.camera != lst[i]:
        activate(context, lst[i])


def sync_project(sc=None):
    """카메라 목록을 앱의 project.json 에 반영 (이름 변경·삭제·Blender 에서 추가한 카메라)"""
    sc = sc or bpy.context.scene
    path = sc.get("kedd_project", "")
    if not path or not os.path.exists(path):
        return
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return
    ox, oy = sc.get("kedd_origin", (0.0, 0.0))[:2]
    by_id = {c.get("id"): c for c in d.get("cameras", []) if c.get("id")}
    out = []
    for ob in cams(sc):
        cid = ob.get("kedd_id")
        if cid and cid in by_id:
            c = dict(by_id[cid])
        else:
            cid = cid or uuid.uuid4().hex[:8]
            ob["kedd_id"] = cid
            c = dict(id=cid, created=ob.get("kedd_created", ""), kind="fixed")
        if c.get("kind") == "fixed":
            l = ob.matrix_world.translation
            c.update(X=l.x + ox, Y=l.y + oy, z=l.z, rotation=list(ob.rotation_euler),
                     fov=ob.data.angle_y * 57.29578, aspect=(ob.get("kedd_res") or [3, 2])[0] / (ob.get("kedd_res") or [3, 2])[1])
        c["name"] = ob.name
        out.append(c)
    d["cameras"] = out
    try:
        tmp = path + ".tmp"
        json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        print("KEDD: project.json 저장 실패", e)


def render_thumb_gl(context, ob, width=480):
    """뷰포트(OpenGL) 렌더로 썸네일 — Blender 안에서 쓰는 빠른 방식"""
    sc = context.scene
    old = (sc.camera, sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage, sc.render.filepath)
    r = ob.get("kedd_res") or [sc.render.resolution_x, sc.render.resolution_y]
    sc.camera = ob
    sc.render.resolution_x, sc.render.resolution_y = width, max(1, int(width * r[1] / r[0]))
    sc.render.resolution_percentage = 100
    d = bpy.path.abspath("//thumbs") if bpy.data.filepath else bpy.app.tempdir
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, ob.name + ".png")
    try:
        bpy.ops.render.opengl(write_still=False, view_context=False)
        bpy.data.images["Render Result"].save_render(p)
    finally:
        sc.camera, sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage, sc.render.filepath = old
    set_thumb(ob, p)


def set_thumb(ob, path):
    nm = "KEDD_thumb_" + ob.get("kedd_id", ob.name)
    im = bpy.data.images.get(nm)
    if im:
        bpy.data.images.remove(im)
    im = bpy.data.images.load(path)
    im.name = nm
    im.pack()
    im.use_fake_user = True
    ob["kedd_thumb"] = im.name


# ------------------------------------------------------------------ UI
class KEDD_UL_cameras(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0, flt_flag=0):
        ob = cams()[index] if index < len(cams()) else None
        if ob is None:
            return
        row = layout.row(align=True)
        ic = icon_of(ob)
        if ic:
            row.label(text="", icon_value=ic)
        else:
            row.label(text="", icon="CAMERA_DATA")
        row.prop(ob, "name", text="", emboss=False)
        if context.scene.camera == ob:
            row.label(text="", icon="CHECKMARK")
        row.label(text=(ob.get("kedd_created", "") or "")[5:16].replace("T", " "))


class KEDD_PG_dummy(bpy.types.PropertyGroup):
    pass


class KEDD_PT_cameras(bpy.types.Panel):
    bl_label = "KEDD 카메라"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "KEDD"

    def draw(self, context):
        sc = context.scene
        lst = cams()
        col = self.layout.column()
        _ensure_items(sc, len(lst))
        col.template_list("KEDD_UL_cameras", "", sc, "kedd_cam_items", sc, "kedd_cam_index", rows=6)
        i = sc.kedd_cam_index
        if 0 <= i < len(lst):
            ob = lst[i]
            ic = icon_of(ob)
            if ic:
                col.template_icon(icon_value=ic, scale=9)
            info = ob.get("kedd_label", "")
            if info:
                col.label(text=info)
        row = col.row(align=True)
        row.operator("kedd.cam_from_view", icon="ADD", text="현재 뷰 추가")
        row.operator("kedd.cam_thumb", icon="FILE_REFRESH", text="썸네일")
        row.operator("kedd.cam_delete", icon="TRASH", text="")
        col.operator("render.render", icon="RENDER_STILL", text="이 카메라로 렌더 (F12)").use_viewport = False


def _ensure_items(sc, n):
    # template_list 는 컬렉션 속성이 필요 → 카메라 수만큼 빈 항목 유지 (draw 에서는 수정 불가하므로 타이머로)
    if len(sc.kedd_cam_items) != n:
        def fix():
            s = bpy.context.scene
            k = len(cams())
            while len(s.kedd_cam_items) < k:
                s.kedd_cam_items.add()
            while len(s.kedd_cam_items) > k:
                s.kedd_cam_items.remove(len(s.kedd_cam_items) - 1)
            return None
        bpy.app.timers.register(fix, first_interval=0.01)


class KEDD_OT_cam_from_view(bpy.types.Operator):
    bl_idname = "kedd.cam_from_view"
    bl_label = "현재 뷰를 카메라로 추가"
    bl_description = "지금 3D 뷰의 시점을 새 카메라로 저장하고 썸네일을 만듭니다"

    def execute(self, context):
        rv = context.space_data.region_3d if context.space_data and context.space_data.type == "VIEW_3D" else None
        if rv is None:
            self.report({"WARNING"}, "3D 뷰에서 실행하세요")
            return {"CANCELLED"}
        sc = context.scene
        n = len(cams()) + 1
        cd = bpy.data.cameras.new(f"Cam_{n:02d}_Blender")
        cd.sensor_fit = "VERTICAL"
        cd.lens = context.space_data.lens
        cd.clip_start, cd.clip_end = 10, 300000
        ob = bpy.data.objects.new(cd.name, cd)
        c = bpy.data.collections.get(COLL) or bpy.data.collections.new(COLL)
        if c.name not in sc.collection.children:
            try:
                sc.collection.children.link(c)
            except Exception:
                pass
        c.objects.link(ob)
        ob.matrix_world = rv.view_matrix.inverted()
        ob["kedd_id"] = uuid.uuid4().hex[:8]
        ob["kedd_created"] = dt.datetime.now().isoformat(timespec="minutes")
        ob["kedd_label"] = "Blender 뷰에서 추가"
        ob["kedd_res"] = [sc.render.resolution_x, sc.render.resolution_y]
        try:
            render_thumb_gl(context, ob)
        except Exception as e:
            print("KEDD: 썸네일 실패", e)
        _ensure_items(sc, len(cams()))
        sc.kedd_cam_index = cams().index(ob)
        return {"FINISHED"}


class KEDD_OT_cam_thumb(bpy.types.Operator):
    bl_idname = "kedd.cam_thumb"
    bl_label = "썸네일 다시 만들기"
    bl_description = "선택한 카메라의 썸네일을 뷰포트 렌더로 다시 만듭니다"

    def execute(self, context):
        lst = cams()
        i = context.scene.kedd_cam_index
        if not (0 <= i < len(lst)):
            return {"CANCELLED"}
        render_thumb_gl(context, lst[i])
        return {"FINISHED"}


class KEDD_OT_cam_delete(bpy.types.Operator):
    bl_idname = "kedd.cam_delete"
    bl_label = "카메라 삭제"
    bl_description = "선택한 카메라와 썸네일을 삭제합니다"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        sc = context.scene
        lst = cams()
        i = sc.kedd_cam_index
        if not (0 <= i < len(lst)):
            return {"CANCELLED"}
        ob = lst[i]
        im = thumb_of(ob)
        cd = ob.data
        bpy.data.objects.remove(ob, do_unlink=True)
        if cd.users == 0:
            bpy.data.cameras.remove(cd)
        if im:
            bpy.data.images.remove(im)
        rest = cams()
        sc.kedd_cam_index = max(0, min(i, len(rest) - 1))
        if rest:
            activate(context, rest[sc.kedd_cam_index])
        return {"FINISHED"}


# ------------------------------------------------------------------ 앱과 연결 (열려 있는 파일 감지)
#   <파일>.blend.kedd_live : Blender 가 1초마다 갱신 (앱은 이걸 보고 '이미 열려 있음' 판단)
#   <파일>.blend.kedd_cmd  : 앱이 쓰는 명령 {"camera": 이름, "revert": true/false}
_state = {"pending": None, "last_live": 0.0}


def _main_window():
    wm = bpy.context.window_manager
    return wm.windows[0] if wm and wm.windows else None


def _notify(msg):
    print("KEDD:", msg)
    win = _main_window()
    if not win:
        return
    try:
        def draw(self, context):
            self.layout.label(text=msg)
        with bpy.context.temp_override(window=win):
            bpy.context.window_manager.popup_menu(draw, title="KEDD CityModel", icon="INFO")
    except Exception:
        pass


def _tick():
    import time
    try:
        fp = bpy.data.filepath
        if not fp:
            return 1.0
        fp = os.path.realpath(fp)
        now = time.time()
        if now - _state["last_live"] > 1.5:
            try:
                with open(fp + ".kedd_live", "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "t": now}, f)
            except Exception:
                pass
            _state["last_live"] = now
        cp = fp + ".kedd_cmd"
        if os.path.exists(cp):
            try:
                cmd = json.load(open(cp, encoding="utf-8"))
            except Exception:
                cmd = {}
            try:
                os.remove(cp)
            except Exception:
                pass
            cam = cmd.get("camera")
            if cmd.get("revert"):
                if bpy.data.is_dirty:
                    _state["pending"] = None
                    _notify("앱에서 모델/카메라가 갱신됐어요. 저장 안 한 변경이 있어 자동으로 다시 불러오지 않았어요 → 파일 > 되돌리기")
                else:
                    _state["pending"] = cam
                    win = _main_window()
                    if win:
                        with bpy.context.temp_override(window=win):
                            bpy.ops.wm.revert_mainfile()
                    return 0.5
            elif cam:
                _state["pending"] = cam
        if _state["pending"]:
            ob = bpy.data.objects.get(_state["pending"])
            if ob is not None and ob.type == "CAMERA":
                ctx = bpy.context
                lst = cams()
                win = _main_window()
                if win:
                    with ctx.temp_override(window=win):
                        if ob in lst:
                            ctx.scene.kedd_cam_index = lst.index(ob)
                        activate(bpy.context, ob)
            _state["pending"] = None
    except Exception as e:
        print("KEDD tick", e)
    return 0.5


@bpy.app.handlers.persistent
def _load_post(*_):
    _state["last_live"] = 0.0


@bpy.app.handlers.persistent
def _save_post(*_):
    try:
        sync_project(bpy.context.scene)
    except Exception as e:
        print("KEDD sync", e)


CLASSES = (KEDD_PG_dummy, KEDD_UL_cameras, KEDD_PT_cameras, KEDD_OT_cam_from_view, KEDD_OT_cam_thumb, KEDD_OT_cam_delete)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.kedd_cam_items = bpy.props.CollectionProperty(type=KEDD_PG_dummy)
    bpy.types.Scene.kedd_cam_index = IntProperty(name="카메라", default=0, update=_on_index)
    if _save_post not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(_save_post)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    if not bpy.app.background and not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    if _save_post in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_save_post)
    del bpy.types.Scene.kedd_cam_index
    del bpy.types.Scene.kedd_cam_items
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


def _open_with_camera(name):
    _state["pending"] = name


if __name__ == "__main__":
    a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    cam = a[a.index("--kedd-camera") + 1] if "--kedd-camera" in a else None
    if not hasattr(bpy.types, "KEDD_PT_cameras"):
        register()
    elif not bpy.app.background:
        # 설치된 애드온이 이미 패널을 등록함 (예전 버전일 수 있음) → 앱 연결 타이머는 이 스크립트가 직접 돌림
        bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)
        if _load_post not in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.append(_load_post)
    if cam:
        _open_with_camera(cam)
