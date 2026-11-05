"""tkinter GUI (Windows / macOS 공통)."""
import os
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

from .config import Settings, PRESETS, APP_NAME, APP_VERSION
from .util import Reporter, Cancelled
from . import blender_runner

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".kedd_citymodel.json")


def _lin2hex(c):
    s = [max(0, min(255, int(round((v ** (1 / 2.2)) * 255)))) for v in c[:3]]
    return "#%02x%02x%02x" % tuple(s)


def _hex2lin(h):
    h = h.lstrip("#")
    return tuple((int(h[i:i + 2], 16) / 255) ** 2.2 for i in (0, 2, 4))


def open_path(p):
    if sys.platform == "darwin":
        subprocess.Popen(["open", p])
    elif sys.platform.startswith("win"):
        os.startfile(p)  # noqa
    else:
        subprocess.Popen(["xdg-open", p])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.minsize(1100, 720)
        self.s = Settings.load(SETTINGS_FILE) if os.path.exists(SETTINGS_FILE) else Settings()
        self.q = queue.Queue()
        self.worker = None
        self.cancel = False
        self.preview_imgs = []
        self._lib_rows = {}
        self._build()
        self._load_to_ui()
        self.after(100, self._poll)
        self.after(800, lambda: self._fetch_catalog(silent=True))
        if not self.s.blender_path:
            b = blender_runner.find_blender()
            if b:
                self.v_blender.set(b)

    # ---------------- UI ----------------
    def _build(self):
        pad = dict(padx=6, pady=3)
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        left = ttk.Frame(root)
        left.pack(side="left", fill="y")
        right = ttk.Frame(root)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.txt = tk.Text(right, height=12, wrap="word", font=("Menlo" if sys.platform == "darwin" else "Consolas", 10))
        self.txt.pack(fill="both", expand=False, pady=(6, 0))
        tab_lib = ttk.Frame(self.nb, padding=6)
        tab_cam = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab_lib, text="건물 데이터")
        self.nb.add(tab_cam, text="카메라")
        self._fixed_tabs = 2

        # 1. 범위
        f = ttk.LabelFrame(left, text="1. 범위 (WGS84 경위도)")
        f.pack(fill="x", **pad)
        self.v_preset = tk.StringVar(value="직접 입력")
        cb = ttk.Combobox(f, textvariable=self.v_preset, values=["직접 입력"] + list(PRESETS), state="readonly", width=30)
        cb.grid(row=0, column=0, columnspan=4, sticky="we", **pad)
        cb.bind("<<ComboboxSelected>>", self._on_preset)
        self.v_bbox = [tk.StringVar() for _ in range(4)]
        for i, (lab, r, c) in enumerate([("서쪽 경도", 1, 0), ("남쪽 위도", 2, 0), ("동쪽 경도", 1, 2), ("북쪽 위도", 2, 2)]):
            ttk.Label(f, text=lab).grid(row=r, column=c, sticky="e", **pad)
            ttk.Entry(f, textvariable=self.v_bbox[i], width=10).grid(row=r, column=c + 1, sticky="w", **pad)
        self.lbl_size = ttk.Label(f, text="", foreground="#666")
        self.lbl_size.grid(row=3, column=0, columnspan=4, sticky="w", **pad)
        for v in self.v_bbox:
            v.trace_add("write", lambda *a: (self._update_size(), self._refresh_lib() if hasattr(self, "_lib_rows") else None))

        # 2. 건물 데이터 (전국 라이브러리)
        f = ttk.LabelFrame(tab_lib, text="건물 데이터 폴더 (브이월드 GIS건물통합정보, 시도별 zip)")
        f.pack(fill="x", **pad)
        self.v_lib = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_lib, width=38).grid(row=0, column=0, columnspan=3, sticky="we", **pad)
        ttk.Button(f, text="…", width=3, command=self._pick_lib).grid(row=0, column=3, **pad)
        tf = ttk.Frame(f)
        tf.grid(row=1, column=0, columnspan=4, sticky="we", **pad)
        self.tv = ttk.Treeview(tf, columns=("need", "name", "latest", "have", "size", "state"), show="headings",
                               height=16, selectmode="extended")
        for c, t, w in [("need", "범위", 40), ("name", "시도", 130), ("latest", "브이월드 최신", 90), ("have", "내 폴더", 90),
                        ("size", "용량", 60), ("state", "상태", 60)]:
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="center" if c != "name" else "w")
        self.tv.tag_configure("dim", foreground="#9a9a9a")
        self.tv.tag_configure("active", foreground="#111111")
        self.tv.tag_configure("needget", foreground="#c0392b")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")
        self.tv.bind("<Double-1>", lambda e: self._download_selected())
        self.tv.bind("<<TreeviewSelect>>", self._on_tv_select)
        bf = ttk.Frame(f)
        bf.grid(row=2, column=0, columnspan=4, sticky="w", **pad)
        for t, cmd in [("목록 새로고침", self._fetch_catalog), ("선택 받기", self._download_selected),
                       ("범위에 필요한 것 받기", self._download_needed), ("다운로드 폴더에서 가져오기", self._import_dl)]:
            ttk.Button(bf, text=t, command=cmd).pack(side="left", padx=(0, 3))
        bf2 = ttk.Frame(f)
        bf2.grid(row=3, column=0, columnspan=4, sticky="w", **pad)
        for t, cmd in [("zip 추가…", self._add_zip), ("폴더 열기", lambda: open_path(self._lib())),
                       ("이전 버전 정리", self._clean_lib)]:
            ttk.Button(bf2, text=t, command=cmd).pack(side="left", padx=(0, 3))
        self.lbl_lib = ttk.Label(f, text="", foreground="#666", wraplength=560)
        self.lbl_lib.grid(row=4, column=0, columnspan=4, sticky="w", **pad)

        # 카메라
        f = ttk.LabelFrame(tab_cam, text="카메라 (선택) — 웹 지도 주소창 URL")
        f.pack(fill="x", **pad)
        self.v_cam = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_cam, width=60).grid(row=0, column=0, sticky="we", **pad)
        ttk.Button(f, text="확인", command=self._check_cam).grid(row=0, column=1, **pad)
        ttk.Button(f, text="기존 .blend 에 적용…", command=self._apply_cam).grid(row=0, column=2, **pad)
        self.lbl_cam = ttk.Label(f, text="구글 지도/어스, 네이버, 카카오, 브이월드 · 비우면 범위에 맞춘 기본 구도", foreground="#666")
        self.lbl_cam.grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        # 3. 출력
        f = ttk.LabelFrame(left, text="2. 출력")
        f.pack(fill="x", **pad)
        self.v_work = tk.StringVar()
        self.v_name = tk.StringVar()
        self.v_blender = tk.StringVar()
        for r, (lab, var, cmd) in enumerate([("작업 폴더", self.v_work, self._pick_work), ("프로젝트 이름", self.v_name, None),
                                             ("Blender", self.v_blender, self._pick_blender)]):
            ttk.Label(f, text=lab).grid(row=r, column=0, sticky="e", **pad)
            ttk.Entry(f, textvariable=var, width=30).grid(row=r, column=1, sticky="we", **pad)
            if cmd:
                ttk.Button(f, text="…", width=3, command=cmd).grid(row=r, column=2, **pad)

        # 4. 옵션
        f = ttk.LabelFrame(left, text="3. 옵션")
        f.pack(fill="x", **pad)
        self.v_exag = tk.DoubleVar()
        self.v_ao = tk.DoubleVar()
        self.v_minor = tk.BooleanVar()
        self.v_osmb = tk.BooleanVar()
        self.v_prev = tk.BooleanVar()
        ttk.Label(f, text="지형 과장").grid(row=0, column=0, sticky="e", **pad)
        ttk.Spinbox(f, from_=1.0, to=4.0, increment=0.1, textvariable=self.v_exag, width=6).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="AO 강도").grid(row=0, column=2, sticky="e", **pad)
        ttk.Spinbox(f, from_=0.0, to=1.0, increment=0.05, textvariable=self.v_ao, width=6).grid(row=0, column=3, sticky="w", **pad)
        ttk.Checkbutton(f, text="이면도로 포함", variable=self.v_minor).grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(f, text="OSM 으로 건물 보완", variable=self.v_osmb).grid(row=1, column=2, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(f, text="완료 후 미리보기 렌더", variable=self.v_prev).grid(row=2, column=0, columnspan=4, sticky="w", **pad)
        cf = ttk.Frame(f)
        cf.grid(row=3, column=0, columnspan=4, sticky="w", **pad)
        self.col_btns = {}
        for key, lab in [("col_green", "녹지"), ("col_water", "물"), ("col_road", "도로"), ("col_building", "건물")]:
            ttk.Label(cf, text=lab).pack(side="left")
            # macOS 기본 버튼은 배경색을 무시하므로 Label 을 색 견본으로 사용
            b = tk.Label(cf, width=4, relief="solid", bd=1, cursor="hand2", text=" ")
            b.pack(side="left", padx=(2, 10), ipady=2)
            b.bind("<Button-1>", lambda e, k=key: self._pick_color(k))
            self.col_btns[key] = b
        ttk.Button(f, text="고급 설정…", command=self._advanced).grid(row=4, column=0, columnspan=4, sticky="w", **pad)

        # 실행
        f = ttk.Frame(left)
        f.pack(fill="x", **pad)
        self.btn_run = ttk.Button(f, text="▶ 생성 시작", command=self._run)
        self.btn_run.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(f, text="중지", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_open = ttk.Button(f, text="결과 폴더", command=lambda: open_path(self.s.project_dir))
        self.btn_open.pack(side="left")
        self.pb = ttk.Progressbar(left, maximum=1000)
        self.pb.pack(fill="x", **pad)

        self._log(f"{APP_NAME} {APP_VERSION}\n범위와 건물 파일을 지정하고 [생성 시작]을 누르세요.\n"
                  "건물 데이터: 목록에서 ★(범위에 필요) 항목을 [범위에 필요한 것 받기] → 받은 뒤 [다운로드 폴더에서 가져오기]")

    # ------------- 이벤트 -------------
    def _on_preset(self, *_):
        p = PRESETS.get(self.v_preset.get())
        if p:
            for v, x in zip(self.v_bbox, p):
                v.set(f"{x:.4f}")
            if not self.v_name.get() or self.v_name.get() in ("seoul",):
                self.v_name.set(self.v_preset.get().split(" ")[0])

    def _update_size(self):
        try:
            a, b, c, d = [float(v.get()) for v in self.v_bbox]
            self.lbl_size.config(text=f"약 {(c - a) * 88:.1f} × {(d - b) * 111:.1f} km")
        except Exception:
            self.lbl_size.config(text="")

    def _lib(self):
        return self.v_lib.get().strip() or self.s.building_lib

    def _pick_lib(self):
        d = filedialog.askdirectory(initialdir=self._lib())
        if d:
            self.v_lib.set(d)
            self._refresh_lib()

    def _cur_bbox(self):
        try:
            return tuple(float(v.get()) for v in self.v_bbox)
        except Exception:
            return None

    def _refresh_lib(self):
        from . import library
        sel = set(self.tv.selection())
        for i in self.tv.get_children():
            self.tv.delete(i)
        lib = self._lib()
        rows, fetched = library.overview(lib, self._cur_bbox())
        self._lib_rows = {r["code"]: r for r in rows}
        for r in rows:
            tag = "dim" if r["state"] == "보유" else ("needget" if r["need"] else "active")
            self.tv.insert("", "end", iid=r["code"], tags=(tag,),
                           values=("★" if r["need"] else "", r["name"], r["latest"], r["have"], r["size"], r["state"]))
        for i in sel:
            if self.tv.exists(i):
                self.tv.selection_add(i)
        need_missing = [r["name"] for r in rows if r["need"] and r["state"] != "보유"]
        msg = f"목록 기준: {fetched}" if fetched else "브이월드 목록을 아직 불러오지 않았습니다 → [목록 새로고침]"
        if need_missing:
            msg += f" · 현재 범위에 필요한데 없는/오래된 데이터: {', '.join(need_missing)}"
        self.lbl_lib.config(text=msg + " · 받기 전에 브라우저에서 브이월드에 로그인하세요.")

    def _on_tv_select(self, *_):
        # 흐린(보유) 항목은 선택되지 않게
        for i in self.tv.selection():
            if self._lib_rows.get(i, {}).get("state") == "보유":
                self.tv.selection_remove(i)

    def _fetch_catalog(self, silent=False):
        from . import library

        def work():
            try:
                cat = library.fetch_catalog(self._lib())
                self.q.put(("log", f"브이월드 목록 갱신: {len(cat)}개 시도"))
            except Exception as e:
                if not silent:
                    self.q.put(("log", f"브이월드 목록을 불러오지 못했습니다: {e}"))
            self.q.put(("refresh_lib", None))
        threading.Thread(target=work, daemon=True).start()

    def _open_urls(self, rows):
        import webbrowser
        if not rows:
            messagebox.showinfo("받기", "받을 항목이 없습니다 (흐린 항목은 이미 최신입니다).")
            return
        if any(r["latest"] == "?" for r in rows):
            webbrowser.open(rows[0]["url"])
            self._log("브이월드 목록을 아직 불러오지 않아 다운로드 페이지를 열었습니다. [목록 새로고침] 후 다시 시도하면 파일로 바로 연결됩니다.")
            return
        total = sum(int(r["size"].rstrip("MB") or 0) for r in rows)
        if not messagebox.askyesno("받기", f"{len(rows)}개 시도 ({total:,}MB)를 브라우저에서 받습니다.\n"
                                         "브라우저에서 브이월드에 로그인되어 있어야 합니다.\n\n"
                                         + "\n".join(f"· {r['name']} {r['latest']} ({r['size']})" for r in rows)):
            return
        for r in rows:
            webbrowser.open(r["url"])
        self._log("받기가 끝나면 [다운로드 폴더에서 가져오기]를 누르세요. (로그인 안내가 뜨면 로그인 후 다시 [선택 받기])")

    def _download_selected(self):
        rows = [self._lib_rows[i] for i in self.tv.selection() if self._lib_rows[i]["state"] != "보유"]
        self._open_urls(rows)

    def _download_needed(self):
        rows = [r for r in self._lib_rows.values() if r["need"] and r["state"] != "보유"]
        self._open_urls(rows)

    def _add_zip(self):
        from . import library
        fs = filedialog.askopenfilenames(title="GIS건물통합정보 zip (AL_D010_시도_날짜.zip)", filetypes=[("zip", "*.zip")])
        if fs:
            got = library.import_files(self._lib(), fs)
            bad = [os.path.basename(f) for f in fs if not library.FNAME.search(os.path.basename(f))]
            self._log(f"건물 데이터 {len(got)}개 추가" + (f" (형식이 다른 파일 제외: {', '.join(bad)})" if bad else ""))
            self._refresh_lib()

    def _import_dl(self):
        from . import library
        found = library.find_in_downloads(self._lib())
        if not found:
            messagebox.showinfo("가져오기", "다운로드 폴더에 새 GIS건물통합정보 zip이 없습니다.")
            return
        names = "\n".join(os.path.basename(p) for p in found)
        if messagebox.askyesno("가져오기", f"다음 파일을 건물 데이터 폴더로 옮길까요?\n\n{names}"):
            got = library.import_files(self._lib(), found, move=True)
            self._log(f"다운로드 폴더에서 {len(got)}개 가져옴")
            self._refresh_lib()

    def _clean_lib(self):
        from . import library
        if messagebox.askyesno("정리", "시도별 최신 파일만 남기고 이전 날짜 zip과 쓰지 않는 인덱스를 삭제할까요?"):
            n = library.remove_old(self._lib())
            self._log(f"이전 버전 {n}개 삭제")
            self._refresh_lib()

    def _check_cam(self):
        from .webcam import parse_url
        try:
            v = parse_url(self.v_cam.get())
            self.lbl_cam.config(text=f"{v['source']}: 위도 {v['lat']:.5f}, 경도 {v['lon']:.5f}, 거리 {v['range']:.0f}m, "
                                     f"방위 {v['heading']:.0f}°, 기울기 {v['tilt']:.0f}°, 화각 {v['fov']:.0f}°", foreground="#06c")
            return v
        except Exception as e:
            self.lbl_cam.config(text=f"해석 실패: {e}", foreground="#c00")
            return None

    def _apply_cam(self):
        if not self._check_cam():
            return
        f = filedialog.askopenfilename(title="카메라를 바꿀 .blend", filetypes=[("Blender", "*.blend")],
                                       initialdir=self.s.project_dir)
        if not f:
            return
        self._ui_to_settings()
        self.s.project_name = os.path.basename(os.path.dirname(f))
        self.s.work_dir = os.path.dirname(os.path.dirname(f))
        if not os.path.exists(os.path.join(self.s.project_dir, "data", "terrain.npz")):
            messagebox.showerror("카메라", "이 앱으로 만든 .blend 만 가능합니다 (옆에 data 폴더가 있어야 함).")
            return

        def work():
            from .pipeline import update_camera
            rep = Reporter(log=lambda m: self.q.put(("log", m)), progress=lambda p: self.q.put(("prog", p)))
            try:
                update_camera(self.s, f, rep)
                self.q.put(("log", "✔ 카메라 적용 완료: " + f))
            except Exception as e:
                self.q.put(("log", "✖ " + str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _pick_work(self):
        d = filedialog.askdirectory(initialdir=self.v_work.get() or os.path.expanduser("~"))
        if d:
            self.v_work.set(d)

    def _pick_blender(self):
        ft = [("Blender", "blender.exe")] if sys.platform.startswith("win") else [("모든 파일", "*")]
        init = "/Applications" if sys.platform == "darwin" else None
        f = filedialog.askopenfilename(title="Blender 실행 파일 (Mac: Blender.app)", filetypes=ft, initialdir=init)
        if f:
            if f.endswith(".app"):
                f = os.path.join(f, "Contents", "MacOS", "Blender")
            self.v_blender.set(f)
            v = blender_runner.blender_version(f)
            self._log(f"Blender 버전: {v or '확인 실패'}")

    def _pick_color(self, key):
        cur = getattr(self.s, key)
        r = colorchooser.askcolor(color=_lin2hex(cur), title="색 선택")
        if r and r[1]:
            setattr(self.s, key, _hex2lin(r[1]))
            self.col_btns[key].config(bg=r[1])

    def _advanced(self):
        w = tk.Toplevel(self)
        w.title("고급 설정")
        pad = dict(padx=6, pady=3)
        vars_ = {}
        rows = [("osm_pbf", "OSM pbf 직접 지정 (비우면 자동)", "file"),
                ("dem_files", "DEM GeoTIFF 직접 지정 (;로 구분, 비우면 자동)", "files"),
                ("dem_res", "지형 격자 (m)", "num"), ("lc_res", "녹지/물 텍스처 해상도 (m)", "num"),
                ("min_building_area", "최소 건물 면적 (m²)", "num"), ("default_height", "높이 없는 건물 기본 높이 (m)", "num"),
                ("road_offset", "도로 판 높이 (m)", "num"), ("ao_distance", "AO 거리 (m)", "num"),
                ("render_w", "렌더 가로 (px)", "int"), ("render_h", "렌더 세로 (px)", "int"), ("samples", "렌더 샘플", "int")]
        for r, (k, lab, typ) in enumerate(rows):
            ttk.Label(w, text=lab).grid(row=r, column=0, sticky="e", **pad)
            val = getattr(self.s, k)
            v = tk.StringVar(value=";".join(val) if isinstance(val, list) else str(val))
            vars_[k] = (v, typ)
            ttk.Entry(w, textvariable=v, width=40).grid(row=r, column=1, **pad)
            if typ in ("file", "files"):
                def pick(v=v, typ=typ):
                    if typ == "file":
                        f = filedialog.askopenfilename(filetypes=[("OSM PBF", "*.pbf")])
                        if f:
                            v.set(f)
                    else:
                        fs = filedialog.askopenfilenames(filetypes=[("GeoTIFF", "*.tif *.tiff")])
                        if fs:
                            v.set(";".join(fs))
                ttk.Button(w, text="…", width=3, command=pick).grid(row=r, column=2, **pad)

        def ok():
            try:
                for k, (v, typ) in vars_.items():
                    x = v.get().strip()
                    if typ == "files":
                        setattr(self.s, k, [p for p in x.split(";") if p.strip()])
                    elif typ == "file":
                        setattr(self.s, k, x)
                    elif typ == "int":
                        setattr(self.s, k, int(float(x)))
                    else:
                        setattr(self.s, k, float(x))
                w.destroy()
            except ValueError as e:
                messagebox.showerror("입력 오류", str(e), parent=w)
        ttk.Button(w, text="확인", command=ok).grid(row=len(rows), column=1, sticky="e", **pad)

    # ------------- 설정 ↔ UI -------------
    def _load_to_ui(self):
        s = self.s
        for v, x in zip(self.v_bbox, s.bbox):
            v.set(f"{x:.4f}")
        self.v_lib.set(s.building_lib)
        self.v_cam.set(s.camera_url)
        self._refresh_lib()
        self.v_work.set(s.work_dir)
        self.v_name.set(s.project_name)
        self.v_blender.set(s.blender_path)
        self.v_exag.set(s.exag)
        self.v_ao.set(s.ao_strength)
        self.v_minor.set(s.include_minor_roads)
        self.v_osmb.set(s.use_osm_buildings)
        self.v_prev.set(s.preview_render)
        for k, b in self.col_btns.items():
            h = _lin2hex(getattr(s, k))
            b.config(bg=h)

    def _ui_to_settings(self):
        s = self.s
        s.lon_min, s.lat_min, s.lon_max, s.lat_max = [float(v.get()) for v in self.v_bbox]
        s.building_lib = self._lib()
        s.building_zips = []
        s.camera_url = self.v_cam.get().strip()
        s.work_dir = self.v_work.get().strip()
        s.project_name = "".join(ch for ch in self.v_name.get().strip() if ch not in '\\/:*?"<>|')
        s.blender_path = self.v_blender.get().strip()
        s.exag = float(self.v_exag.get())
        s.ao_strength = float(self.v_ao.get())
        s.include_minor_roads = bool(self.v_minor.get())
        s.use_osm_buildings = bool(self.v_osmb.get())
        s.preview_render = bool(self.v_prev.get())

    # ------------- 실행 -------------
    def _log(self, msg):
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")

    def _run(self):
        try:
            self._ui_to_settings()
        except ValueError:
            messagebox.showerror("입력 오류", "범위/숫자 값을 확인하세요.")
            return
        errs = self.s.validate()
        if errs:
            messagebox.showerror("확인 필요", "\n".join(errs))
            return
        if self.s.blender_path and not os.path.isfile(self.s.blender_path):
            messagebox.showerror("Blender", "Blender 실행 파일 경로가 올바르지 않습니다.")
            return
        try:
            self.s.save(SETTINGS_FILE)
        except Exception:
            pass
        self.cancel = False
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.pb["value"] = 0
        self._log("─" * 40)

        def work():
            from .pipeline import run
            rep = Reporter(log=lambda m: self.q.put(("log", m)), progress=lambda p: self.q.put(("prog", p)),
                           cancel_flag=lambda: self.cancel)
            try:
                out, previews = run(self.s, rep, cancel=lambda: self.cancel)
                self.q.put(("done", (out, previews)))
            except Cancelled:
                self.q.put(("log", "■ 중지됨"))
                self.q.put(("done", None))
            except Exception as e:
                self.q.put(("log", "✖ 오류: " + str(e)))
                self.q.put(("log", traceback.format_exc()))
                self.q.put(("done", None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self):
        self.cancel = True
        self._log("중지 요청…")

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._log(val)
                elif kind == "refresh_lib":
                    self._refresh_lib()
                elif kind == "prog":
                    self.pb["value"] = val * 1000
                elif kind == "done":
                    self.btn_run.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self._refresh_lib()
                    if val:
                        out, previews = val
                        self._show_previews(previews)
                        if out and messagebox.askyesno("완료", f"생성 완료:\n{out}\n\n결과 폴더를 열까요?"):
                            open_path(os.path.dirname(out))
        except queue.Empty:
            pass
        self.after(100, self._poll)
        self.after(800, lambda: self._fetch_catalog(silent=True))

    def _show_previews(self, paths):
        from PIL import Image, ImageTk
        for t in self.nb.tabs()[self._fixed_tabs:]:
            self.nb.forget(t)
        self.preview_imgs = []
        names = {"preview_terrain": "지형", "preview_landcover": "녹지·물", "preview_render": "렌더"}
        self.update_idletasks()
        W = max(400, self.nb.winfo_width() - 10)
        H = max(300, self.nb.winfo_height() - 30)
        for p in paths:
            im = Image.open(p)
            im.thumbnail((W, H))
            ph = ImageTk.PhotoImage(im)
            self.preview_imgs.append(ph)
            lab = ttk.Label(self.nb, image=ph)
            key = os.path.splitext(os.path.basename(p))[0]
            self.nb.add(lab, text=names.get(key, key))
        if paths:
            self.nb.select(self._fixed_tabs)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
