"""설치된 Blender 찾기 + 백그라운드 실행."""
import glob
import os
import re
import shutil
import subprocess
import sys
from .util import Reporter, Cancelled


def _ver(p):
    m = re.search(r"(\d+)\.(\d+)", p)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_blender():
    cands = []
    if sys.platform == "darwin":
        cands += ["/Applications/Blender.app/Contents/MacOS/Blender",
                  os.path.expanduser("~/Applications/Blender.app/Contents/MacOS/Blender")]
        cands += glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
    elif sys.platform.startswith("win"):
        for base in [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]:
            cands += sorted(glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe")), key=_ver, reverse=True)
        cands += glob.glob(r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe")
        cands += sorted(glob.glob(os.path.expanduser(r"~\AppData\Local\Programs\Blender Foundation\Blender*\blender.exe")), key=_ver, reverse=True)
    w = shutil.which("blender")
    if w:
        cands.append(w)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def blender_version(path):
    try:
        out = subprocess.run([path, "-b", "--version"], capture_output=True, text=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        m = re.search(r"Blender (\d+\.\d+(\.\d+)?)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def scene_script_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "blender_scene.py")
    if not os.path.exists(p):
        p = os.path.join(base, "citymodel", "blender_scene.py")
    return p


def _copy_script(dst_dir):
    script = os.path.join(dst_dir, "blender_scene.py")
    shutil.copyfile(scene_script_path(), script)
    return script


def _stream(cmd, rep, cancel, steps=30):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tail, done = [], 0
    for line in p.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-30:]
        if line.startswith(("loaded", "SAVED", "PREVIEW")) or "Error" in line or "Traceback" in line:
            rep.log("   [Blender] " + line)
            done += 1
            rep.progress(min(0.95, done / max(steps, 1)))
        if cancel():
            p.kill()
            raise Cancelled()
    p.wait()
    return p.returncode, tail


def run_scene(blender, scene_json, out_blend, rep: Reporter, preview=None, cancel=lambda: False, ntiles=10):
    script = _copy_script(os.path.dirname(scene_json))
    cmd = [blender, "-b", "--factory-startup", "--python", script, "--", "--scene", scene_json, "--out", out_blend]
    if preview:
        cmd += ["--preview", preview]
    tmp = out_blend + ".building"
    rc, tail = _stream(cmd, rep, cancel, steps=ntiles + 2)
    if rc != 0 or not os.path.exists(out_blend):
        raise RuntimeError("Blender 실행 실패:\n" + "\n".join(tail[-12:]))
    rep.progress(1.0)


def run_cameras(blender, blend, cams_json, rep: Reporter, cancel=lambda: False):
    script = _copy_script(os.path.dirname(cams_json))
    cmd = [blender, "-b", blend, "--python", script, "--", "--cameras", cams_json]
    rc, tail = _stream(cmd, rep, cancel, steps=2)
    if rc != 0 or not any("SAVED" in t for t in tail):
        raise RuntimeError("카메라 추가 실패:\n" + "\n".join(tail[-12:]))
    rep.progress(1.0)
