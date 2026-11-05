# Blender 안에서 실행되는 장면 생성 스크립트 (Blender 3.3 ~ 5.x)
#   blender -b --factory-startup --python blender_scene.py -- --data <폴더> --out <파일.blend> [--preview <png>]
# <폴더> 에는 terrain.npz, landcover.png, bld_*.npz, rd_*.npz, br_*.npz, scene.json 이 있어야 함
import bpy, glob, os, sys, json, math
import numpy as np
from mathutils import Vector


def args():
    a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    d = {}
    for k, v in zip(a[::2], a[1::2]):
        d[k.lstrip("-")] = v
    return d


A = args()
DATA = A["data"]
CFG = json.load(open(os.path.join(DATA, "scene.json"), encoding="utf-8"))


def rgba(c):
    return tuple(c[:3]) + (1.0,)


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for m in list(bpy.data.meshes):
        bpy.data.meshes.remove(m)


def get_coll(name):
    c = bpy.data.collections.get(name)
    if not c:
        c = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(c)
    return c


def make_mesh(name, v, loops, ptot, coll, smooth=False):
    me = bpy.data.meshes.new(name)
    me.vertices.add(len(v))
    me.vertices.foreach_set("co", np.ascontiguousarray(v, dtype=np.float32).ravel())
    me.loops.add(len(loops))
    me.loops.foreach_set("vertex_index", np.ascontiguousarray(loops, dtype=np.int32))
    me.polygons.add(len(ptot))
    starts = np.zeros(len(ptot), np.int32)
    starts[1:] = np.cumsum(ptot)[:-1]
    me.polygons.foreach_set("loop_start", starts)
    try:
        me.polygons.foreach_set("loop_total", np.ascontiguousarray(ptot, dtype=np.int32))
    except Exception:
        pass
    me.update(calc_edges=True)
    me.polygons.foreach_set("use_smooth", np.full(len(ptot), smooth, bool))
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    return ob


def set_spec(bsdf, v):
    for k in ("Specular", "Specular IOR Level"):
        if k in bsdf.inputs:
            bsdf.inputs[k].default_value = v
            return


def material(name, color, rough=0.85):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes.get("Principled BSDF")
    b.inputs["Base Color"].default_value = rgba(color)
    b.inputs["Roughness"].default_value = rough
    set_spec(b, 0.2)
    m.diffuse_color = rgba(color)
    return m


def ground_material():
    m = bpy.data.materials.new("Ground")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    tex = nt.nodes.new("ShaderNodeTexImage")
    img = bpy.data.images.load(os.path.join(DATA, "landcover.png"), check_existing=True)
    img.colorspace_settings.name = "Non-Color"
    tex.image = img
    sep = nt.nodes.new("ShaderNodeSeparateRGB") if hasattr(bpy.types, "ShaderNodeSeparateRGB") else nt.nodes.new("ShaderNodeSeparateColor")
    mg = nt.nodes.new("ShaderNodeMixRGB")
    mg.label = "녹지"
    mg.inputs[1].default_value = rgba(CFG["col_ground"])
    mg.inputs[2].default_value = rgba(CFG["col_green"])
    mw = nt.nodes.new("ShaderNodeMixRGB")
    mw.label = "물"
    mw.inputs[2].default_value = rgba(CFG["col_water"])
    rough = nt.nodes.new("ShaderNodeMath")
    rough.operation = "MULTIPLY_ADD"
    rough.inputs[1].default_value = -0.65
    rough.inputs[2].default_value = 0.9
    L = nt.links
    L.new(tex.outputs["Color"], sep.inputs[0])
    L.new(sep.outputs[0], mg.inputs["Fac"])
    L.new(mg.outputs["Color"], mw.inputs[1])
    L.new(sep.outputs[1], mw.inputs["Fac"])
    L.new(sep.outputs[1], rough.inputs[0])
    L.new(mw.outputs["Color"], bsdf.inputs["Base Color"])
    L.new(rough.outputs["Value"], bsdf.inputs["Roughness"])
    set_spec(bsdf, 0.25)
    L.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    for i, n in enumerate([tex, sep, mg, mw, bsdf, out]):
        n.location = (i * 250 - 600, 0)
    return m


def setup_ao(sc, world_col, world_str):
    bpy.context.view_layer.use_pass_ambient_occlusion = True
    sc.world.light_settings.distance = CFG["ao_distance"]
    sc.render.use_compositing = True
    sc.render.film_transparent = True
    sky = tuple(c * world_str for c in world_col) + (1.0,)
    new5 = hasattr(sc, "compositing_node_group")
    if new5:
        nt = bpy.data.node_groups.new("AO_Composite", "CompositorNodeTree")
        nt.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    else:
        sc.use_nodes = True
        nt = sc.node_tree
        nt.nodes.clear()
    N, Lk = nt.nodes, nt.links
    rl = N.new("CompositorNodeRLayers")
    rl.scene = sc
    ao = rl.outputs["Ambient Occlusion"] if "Ambient Occlusion" in rl.outputs else rl.outputs["AO"]
    fac = N.new("ShaderNodeMath" if new5 else "CompositorNodeMath")
    fac.operation = "MULTIPLY"
    fac.label = "AO 강도"
    fac.inputs[1].default_value = CFG["ao_strength"]
    Lk.new(rl.outputs["Alpha"], fac.inputs[0])
    if new5:
        mix = N.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        Lk.new(fac.outputs[0], mix.inputs[0])
        Lk.new(rl.outputs["Image"], mix.inputs[6])
        Lk.new(ao, mix.inputs[7])
        mix_out = mix.outputs[2]
    else:
        mix = N.new("CompositorNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        Lk.new(fac.outputs[0], mix.inputs[0])
        Lk.new(rl.outputs["Image"], mix.inputs[1])
        Lk.new(ao, mix.inputs[2])
        mix_out = mix.outputs[0]
    sa = N.new("CompositorNodeSetAlpha")
    Lk.new(mix_out, sa.inputs["Image"])
    Lk.new(rl.outputs["Alpha"], sa.inputs["Alpha"])
    bg = N.new("CompositorNodeRGB")
    bg.outputs[0].default_value = sky
    ov = N.new("CompositorNodeAlphaOver")
    Lk.new(bg.outputs[0], ov.inputs["Background"] if "Background" in ov.inputs else ov.inputs[1])
    Lk.new(sa.outputs[0], ov.inputs["Foreground"] if "Foreground" in ov.inputs else ov.inputs[2])
    if new5:
        out = N.new("NodeGroupOutput")
        Lk.new(ov.outputs[0], out.inputs[0])
        sc.compositing_node_group = nt
    else:
        comp = N.new("CompositorNodeComposite")
        Lk.new(ov.outputs[0], comp.inputs[0])


def add_camera(sc, cam_cfg, name):
    cd = bpy.data.cameras.new(name)
    cd.sensor_fit = "VERTICAL"
    cd.angle_y = math.radians(cam_cfg.get("fov", 35.0))
    cd.clip_start = 10
    cd.clip_end = 300000
    cam = bpy.data.objects.new(name, cd)
    sc.collection.objects.link(cam)
    cam.location = Vector(cam_cfg["location"])
    cam.rotation_euler = cam_cfg["rotation"]
    sc.camera = cam
    return cam


def build():
    clear_scene()
    sc = bpy.context.scene
    t = np.load(os.path.join(DATA, "terrain.npz"))
    z = t["z"].astype(np.float32)
    res = float(t["res"])
    x0 = float(t["x0"])
    y0 = float(t["y0"])
    H, W = z.shape
    E = CFG["exag"]

    # 지형
    xs = x0 + (np.arange(W) + 0.5) * res
    ys = y0 - (np.arange(H) + 0.5) * res
    X, Y = np.meshgrid(xs, ys)
    V = np.c_[X.ravel(), Y.ravel(), z.ravel() * E]
    i = (np.arange(H - 1)[:, None] * W + np.arange(W - 1)[None, :]).ravel()
    loops = np.c_[i, i + W, i + W + 1, i + 1].ravel()
    ter = make_mesh("Terrain", V, loops, np.full(len(i), 4, np.int32), get_coll("Terrain"), smooth=True)
    me = ter.data
    uvl = me.uv_layers.new(name="UVMap")
    li = np.zeros(len(me.loops), np.int32)
    me.loops.foreach_get("vertex_index", li)
    U = (V[:, 0] - x0) / (W * res)
    Vv = (V[:, 1] - (y0 - H * res)) / (H * res)
    uvl.data.foreach_set("uv", np.c_[U[li], Vv[li]].astype(np.float32).ravel())
    me.materials.append(ground_material())
    print("terrain", W, H, flush=True)

    mb = material("Clay_Building", CFG["col_building"])
    mr = material("Road", CFG["col_road"], 0.7)
    rc = get_coll("Roads_Bridges")
    for f in sorted(glob.glob(os.path.join(DATA, "rd_*.npz")) + glob.glob(os.path.join(DATA, "br_*.npz"))):
        nm = os.path.basename(f)[:-4]
        d = np.load(f)
        ob = make_mesh(nm, d["v"], d["loops"], d["ptot"], rc)
        if nm.startswith("br_"):
            ob.data.materials.append(mb)
            ob.data.materials.append(mr)
            if "mat" in d.files:
                ob.data.polygons.foreach_set("material_index", d["mat"].astype(np.int32))
        else:
            ob.data.materials.append(mr)
        print("loaded", nm, len(d["v"]), flush=True)
    bc = get_coll("Buildings")
    for f in sorted(glob.glob(os.path.join(DATA, "bld_*.npz"))):
        d = np.load(f)
        ob = make_mesh(os.path.basename(f)[:-4], d["v"], d["loops"], d["ptot"], bc)
        ob.data.materials.append(mb)
        print("loaded", os.path.basename(f), len(d["v"]), flush=True)

    # 카메라
    add_camera(sc, CFG["camera"], "Camera")

    # 조명·월드
    sd = bpy.data.lights.new("Sun", "SUN")
    sd.energy = 4.5
    sd.angle = math.radians(4)
    sun = bpy.data.objects.new("Sun", sd)
    sc.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(58), 0, math.radians(-50))
    world_col, world_str = (0.88, 0.89, 0.91), 0.45
    w = sc.world or bpy.data.worlds.new("World")
    sc.world = w
    w.use_nodes = True
    bg = w.node_tree.nodes.get("Background")
    bg.inputs["Color"].default_value = world_col + (1,)
    bg.inputs["Strength"].default_value = world_str

    # 렌더
    sc.render.engine = "CYCLES"
    sc.cycles.samples = CFG["samples"]
    sc.cycles.use_denoising = True
    try:
        sc.cycles.device = "GPU"
    except Exception:
        pass
    sc.render.resolution_x, sc.render.resolution_y = CFG["render_w"], CFG["render_h"]
    try:
        sc.view_settings.view_transform = "Filmic"
    except Exception:
        pass
    for look in ("Filmic - Medium High Contrast", "Medium High Contrast"):
        try:
            sc.view_settings.look = look
            break
        except Exception:
            pass
    sc.unit_settings.system = "METRIC"
    setup_ao(sc, world_col, world_str)

    # 3D 뷰 클리핑 (UI 가 있을 때)
    for scr in bpy.data.screens:
        for ar in scr.areas:
            if ar.type == "VIEW_3D":
                for sp in ar.spaces:
                    if sp.type == "VIEW_3D":
                        sp.clip_start, sp.clip_end = 10, 300000


if A.get("camera"):
    # 기존 .blend 의 카메라만 교체: 새 카메라를 추가하고 활성 카메라로 지정
    cam_cfg = json.load(open(A["camera"], encoding="utf-8"))
    sc = bpy.context.scene
    n = 1 + len([o for o in bpy.data.objects if o.type == "CAMERA"])
    add_camera(sc, cam_cfg, f"Camera_{cam_cfg.get('source', 'web')}_{n}")
    bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath, compress=True)
    print("SAVED", bpy.data.filepath, flush=True)
    raise SystemExit(0)

build()
for im in bpy.data.images:
    if im.filepath:
        im.pack()
out = A.get("out")
if out:
    bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
    print("SAVED", out, flush=True)
pv = A.get("preview")
if pv:
    sc = bpy.context.scene
    sc.render.resolution_x, sc.render.resolution_y = 1200, 800
    sc.cycles.samples = 16
    sc.render.filepath = pv
    bpy.ops.render.render(write_still=True)
    print("PREVIEW", pv, flush=True)
