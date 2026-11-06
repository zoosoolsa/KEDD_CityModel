# -*- mode: python ; coding: utf-8 -*-
# 빌드:  pyinstaller packaging/citymodel.spec --noconfirm
import sys, os
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas, binaries, hidden = [], [], []
for pkg in ("pyproj", "osmium", "imagecodecs", "shapely", "certifi", "keyring", "webview"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas += [(os.path.join(ROOT, "citymodel", "blender_scene.py"), "."), (os.path.join(ROOT, "citymodel", "sido.json"), "."), (os.path.join(ROOT, "citymodel", "map.html"), "."), (os.path.join(ROOT, "citymodel", "kedd_cameras.py"), ".")]
hidden += ["scipy.ndimage", "PIL.ImageTk", "tkinter.colorchooser", "keyring.backends.macOS", "keyring.backends.Windows"]

a = Analysis([os.path.join(ROOT, "run_citymodel.py")], pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=["matplotlib", "pandas", "IPython", "pytest"], noarchive=False)
pyz = PYZ(a.pure)
icon = None
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="KEDD-CityModel", console=False,
          argv_emulation=(sys.platform == "darwin"), icon=icon)
coll = COLLECT(exe, a.binaries, a.datas, name="KEDD-CityModel")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="KEDD-CityModel.app", bundle_identifier="kr.kedd.citymodel",
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": "2.6.2"})
