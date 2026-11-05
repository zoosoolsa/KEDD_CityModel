# Blender 안에서 실행되는 장면 조립 스크립트 (Blender 3.3 ~ 5.x)
#   새 장면:   blender -b --factory-startup --python blender_scene.py -- --scene scene.json --out 파일.blend [--preview png]
#   카메라 추가: blender -b 파일.blend --python blender_scene.py -- --cameras cams.json
import bpy, os, sys, json, math
import numpy as np
from mathutils import Vector


def args():
    a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return {k.lstrip("-"): v for k, v in zip(a[::2], a[1::2])}


A = args()


def rgba(c):
    return tuple(c[:3]) + (1.0,)


def get_coll(name, parent=None):
    c = bpy.data.collections.get(name)
    if not c:
        c = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(c)
    return c


def make_mesh(name, v, loops, ptot, coll, smooth=False, loc=(0, 0, 0)):
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
    ob.location = loc
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


def ground_material(name, img_path, cfg):
    """지면 텍스처 RGBA: R=녹지, G=물, B=주요 도로, A=이면도로"""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    tex = nt.nodes.new("ShaderNodeTexImage")
    img = bpy.data.images.load(img_path, check_existing=True)
    img.colorspace_settings.name = "Non-Color"
    try:
        img.alpha_mode = "CHANNEL_PACKED"
    except Exception:
        pass
    tex.image = img
    tex.extension = "EXTEND"
    tex.interpolation = "Linear"
    sep = nt.nodes.new("ShaderNodeSeparateRGB") if hasattr(bpy.types, "ShaderNodeSeparateRGB") else nt.nodes.new("ShaderNodeSeparateColor")
    L = nt.links
    L.new(tex.outputs["Color"], sep.inputs[0])

    def sharp(sock):
        """부드러운 가장자리 값을 0.3~0.7 구간으로 좁혀 경계를 또렷하게 (해상도 이상의 선명도)"""
        mr_ = nt.nodes.new("ShaderNodeMapRange")
        mr_.inputs[1].default_value = 0.3
        mr_.inputs[2].default_value = 0.7
        mr_.clamp = True
        L.new(sock, mr_.inputs[0])
        return mr_.outputs[0]

    def mix(label, a_socket, color, fac):
        mx = nt.nodes.new("ShaderNodeMixRGB")
        mx.label = label
        mx.name = label
        if a_socket is None:
            mx.inputs[1].default_value = rgba(cfg["col_ground"])
        else:
            L.new(a_socket, mx.inputs[1])
        mx.inputs[2].default_value = rgba(color)
        if fac is None:
            mx.inputs["Fac"].default_value = 0.0
        else:
            L.new(sharp(fac), mx.inputs["Fac"])
        return mx

    mg = mix("녹지", None, cfg["col_green"], sep.outputs[0])
    m2 = mix("이면도로", mg.outputs["Color"], cfg["col_road"],
             tex.outputs["Alpha"] if cfg.get("minor_roads", True) else None)
    m1 = mix("도로", m2.outputs["Color"], cfg["col_road"], sep.outputs[2])
    mw = mix("물", m1.outputs["Color"], cfg["col_water"], sep.outputs[1])
    rough = nt.nodes.new("ShaderNodeMath")
    rough.operation = "MULTIPLY_ADD"
    rough.inputs[1].default_value = -0.65
    rough.inputs[2].default_value = 0.9
    L.new(sep.outputs[1], rough.inputs[0])
    L.new(mw.outputs["Color"], bsdf.inputs["Base Color"])
    L.new(rough.outputs["Value"], bsdf.inputs["Roughness"])
    set_spec(bsdf, 0.25)
    L.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    for i, n in enumerate([tex, sep, mg, m2, m1, mw, bsdf, out]):
        n.location = (i * 230 - 800, 0)
    return m


def terrain_tile(name, d, loc, exag, cfg, coll):
    t = np.load(os.path.join(d, "terrain.npz"))
    z = t["z"].astype(np.float32)
    zp = t["zpad"].astype(np.float32)
    res = float(t["res"])
    H, W = z.shape
    T = (W - 1) * res
    xs = np.arange(W) * res
    ys = T - np.arange(H) * res
    X, Y = np.meshgrid(xs, ys)
    V = np.c_[X.ravel(), Y.ravel(), z.ravel() * exag]
    i = (np.arange(H - 1)[:, None] * W + np.arange(W - 1)[None, :]).ravel()
    loops = np.c_[i, i + W, i + W + 1, i + 1].ravel()
    ob = make_mesh(name, V, loops, np.full(len(i), 4, np.int32), coll, smooth=True, loc=loc)
    me = ob.data
    uvl = me.uv_layers.new(name="UVMap")
    li = np.zeros(len(me.loops), np.int32)
    me.loops.foreach_get("vertex_index", li)
    uvl.data.foreach_set("uv", np.c_[V[li, 0] / T, V[li, 1] / T].astype(np.float32).ravel())
    # 이웃 타일과 이음매 없이 이어지도록 여유 칸(zpad)으로 법선 계산
    dzdx = (zp[1:-1, 2:] - zp[1:-1, :-2]) * exag / (2 * res)
    dzdy = (zp[:-2, 1:-1] - zp[2:, 1:-1]) * exag / (2 * res)
    n = np.dstack([-dzdx, -dzdy, np.ones_like(dzdx)]).reshape(-1, 3)
    n /= np.linalg.norm(n, axis=1)[:, None]
    try:
        if hasattr(me, "use_auto_smooth"):
            me.use_auto_smooth = True
        me.normals_split_custom_set_from_vertices([tuple(v) for v in n])
    except Exception as e:
        print("normals skip", e)
    me.materials.append(ground_material(name + "_mat", os.path.join(d, "landcover.png"), cfg))
    return ob


def add_camera(sc, cam, name, cfg):
    cd = bpy.data.cameras.new(name)
    cd.sensor_fit = "VERTICAL"
    cd.angle_y = math.radians(cam.get("fov", 35.0))
    cd.clip_start = 10
    cd.clip_end = 300000
    ob = bpy.data.objects.new(name, cd)
    get_coll("Cameras").objects.link(ob)
    ob.location = Vector(cam["location"])
    ob.rotation_euler = cam["rotation"]
    sc.camera = ob
    asp = cam.get("aspect")
    if asp:
        sc.render.resolution_x = cfg.get("render_w", 3000)
        sc.render.resolution_y = int(round(sc.render.resolution_x / asp))
    return ob


def setup_ao(sc, cfg, world_col, world_str):
    bpy.context.view_layer.use_pass_ambient_occlusion = True
    sc.world.light_settings.distance = cfg["ao_distance"]
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
    fac.inputs[1].default_value = cfg["ao_strength"]
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


def build(cfg):
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for m in list(bpy.data.meshes):
        bpy.data.meshes.remove(m)
    sc = bpy.context.scene
    E = cfg["exag"]
    ox, oy = cfg["origin"]
    mb = material("Clay_Building", cfg["col_building"])
    mr = material("Road", cfg["col_road"], 0.7)
    c_ter = get_coll("Terrain")
    c_bld = get_coll("Buildings")
    c_rd = get_coll("Roads_Bridges")
    for ti, t in enumerate(cfg["tiles"]):
        d, tx, ty = t["dir"], t["x0"], t["y0"]
        nm = os.path.basename(d)
        loc = (tx - ox, ty - oy, 0.0)
        terrain_tile("T_" + nm, d, loc, E, cfg, c_ter)
        for kind, coll in (("bld", c_bld), ("rd_major", c_rd), ("rd_minor", c_rd), ("br", c_rd)):
            p = os.path.join(d, kind + ".npz")
            if not os.path.exists(p) or (kind == "rd_minor" and not cfg.get("minor_roads", True)):
                continue
            z = np.load(p)
            ob = make_mesh(f"{kind}_{nm}", z["v"], z["loops"], z["ptot"], coll, loc=loc)
            if kind == "br":
                ob.data.materials.append(mb)
                ob.data.materials.append(mr)
                if "mat" in z.files:
                    ob.data.polygons.foreach_set("material_index", z["mat"].astype(np.int32))
            else:
                ob.data.materials.append(mr if kind.startswith("rd") else mb)
        print(f"loaded tile {ti + 1}/{len(cfg['tiles'])} {nm}", flush=True)

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

    sc.render.engine = "CYCLES"
    sc.cycles.samples = cfg["samples"]
    sc.cycles.use_denoising = True
    try:
        sc.cycles.device = "GPU"
    except Exception:
        pass
    sc.render.resolution_x, sc.render.resolution_y = cfg["render_w"], cfg["render_h"]
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
    setup_ao(sc, cfg, world_col, world_str)
    for i, cam in enumerate(cfg["cameras"]):
        add_camera(sc, cam, cam.get("name") or f"Cam_{i + 1:02d}", cfg)
    for scr in bpy.data.screens:
        for ar in scr.areas:
            if ar.type == "VIEW_3D":
                for sp in ar.spaces:
                    if sp.type == "VIEW_3D":
                        sp.clip_start, sp.clip_end = 10, 300000


if A.get("cameras"):
    cams = json.load(open(A["cameras"], encoding="utf-8"))
    cfg = cams.get("cfg", {})
    for cam in cams["cameras"]:
        add_camera(bpy.context.scene, cam, cam.get("name") or "Camera", cfg)
    bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath, compress=True)
    print("SAVED", bpy.data.filepath, flush=True)
else:
    CFG = json.load(open(A["scene"], encoding="utf-8"))
    build(CFG)
    for im in bpy.data.images:
        if im.filepath:
            im.pack()
    if A.get("out"):
        bpy.ops.wm.save_as_mainfile(filepath=A["out"], compress=True)
        print("SAVED", A["out"], flush=True)
    if A.get("preview"):
        sc = bpy.context.scene
        sc.render.resolution_percentage = 40
        sc.cycles.samples = 16
        sc.render.filepath = A["preview"]
        bpy.ops.render.render(write_still=True)
        print("PREVIEW", A["preview"], flush=True)
