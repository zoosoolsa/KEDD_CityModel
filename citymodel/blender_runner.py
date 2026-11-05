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


def run_blender(blender, data_dir, out_blend, rep: Reporter, preview=None, cancel=lambda: False):
    script = os.path.join(data_dir, "blender_scene.py")
    shutil.copyfile(scene_script_path(), script)
    cmd = [blender, "-b", "--factory-startup", "--python", script, "--", "--data", data_dir, "--out", out_blend]
    if preview:
        cmd += ["--preview", preview]
    rep.log("   " + " ".join(f'"{c}"' if " " in c else c for c in cmd[:5]) + " …")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    steps, done = 25, 0
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-30:]
        if line.startswith(("loaded", "terrain", "SAVED", "PREVIEW")) or "Error" in line or "Traceback" in line:
            rep.log("   [Blender] " + line)
            done += 1
            rep.progress(min(0.95, done / steps))
        elif line.startswith("Fra:") and "Sample" in line:
            m = re.search(r"Sample (\d+)/(\d+)", line)
            if m:
                rep.progress(0.8 + 0.2 * int(m.group(1)) / int(m.group(2)))
        if cancel():
            p.kill()
            raise Cancelled()
    p.wait()
    if p.returncode != 0 or not os.path.exists(out_blend):
        raise RuntimeError("Blender 실행 실패:\n" + "\n".join(tail[-12:]))
    rep.progress(1.0)


def run_camera_update(blender, data_dir, blend, cam_json, rep: Reporter):
    script = os.path.join(data_dir, "blender_scene.py")
    shutil.copyfile(scene_script_path(), script)
    cmd = [blender, "-b", blend, "--python", script, "--", "--data", data_dir, "--camera", cam_json]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if "SAVED" not in p.stdout:
        raise RuntimeError("카메라 변경 실패:\n" + "\n".join((p.stdout + p.stderr).splitlines()[-12:]))
    rep.log("   카메라를 추가하고 활성 카메라로 지정했습니다 (기존 카메라는 그대로 남아 있음)")
