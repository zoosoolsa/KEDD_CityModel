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
        self.minsize(900, 680)
        self.s = Settings.load(SETTINGS_FILE) if os.path.exists(SETTINGS_FILE) else Settings()
        self.q = queue.Queue()
        self.worker = None
        self.cancel = False
        self.preview_imgs = []
        self._build()
        self._load_to_ui()
        self.after(100, self._poll)
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
            v.trace_add("write", lambda *a: self._update_size())

        # 2. 입력 데이터
        f = ttk.LabelFrame(left, text="2. 데이터")
        f.pack(fill="x", **pad)
        ttk.Label(f, text="브이월드 GIS건물통합정보 (시도별 zip, 여러 개 가능)").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        self.lst_zip = tk.Listbox(f, height=4, width=44)
        self.lst_zip.grid(row=1, column=0, columnspan=2, sticky="we", **pad)
        bf = ttk.Frame(f)
        bf.grid(row=1, column=2, sticky="n")
        ttk.Button(bf, text="추가…", command=self._add_zip).pack(fill="x")
        ttk.Button(bf, text="삭제", command=self._del_zip).pack(fill="x", pady=2)
        ttk.Label(f, text="지형·OSM 은 자동 다운로드됩니다 (직접 지정하려면 고급 설정)", foreground="#666").grid(row=2, column=0, columnspan=3, sticky="w", **pad)

        # 3. 출력
        f = ttk.LabelFrame(left, text="3. 출력")
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
        f = ttk.LabelFrame(left, text="4. 옵션")
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

        # 오른쪽: 미리보기 + 로그
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.txt = tk.Text(right, height=12, wrap="word", font=("Menlo" if sys.platform == "darwin" else "Consolas", 10))
        self.txt.pack(fill="both", expand=False, pady=(6, 0))
        self._log(f"{APP_NAME} {APP_VERSION}\n범위와 건물 파일을 지정하고 [생성 시작]을 누르세요.\n"
                  "건물 파일: 브이월드 > 공간정보 다운로드 > GIS건물통합정보 > 시도 '전체데이터' (AL_D010_xx_날짜.zip)")

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

    def _add_zip(self):
        fs = filedialog.askopenfilenames(title="GIS건물통합정보 zip 선택", filetypes=[("zip / shp", "*.zip *.shp"), ("모든 파일", "*.*")])
        for f in fs:
            if f not in self.lst_zip.get(0, "end"):
                self.lst_zip.insert("end", f)

    def _del_zip(self):
        for i in reversed(self.lst_zip.curselection()):
            self.lst_zip.delete(i)

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
        self.lst_zip.delete(0, "end")
        for p in s.building_zips:
            self.lst_zip.insert("end", p)
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
        s.building_zips = list(self.lst_zip.get(0, "end"))
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
                elif kind == "prog":
                    self.pb["value"] = val * 1000
                elif kind == "done":
                    self.btn_run.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    if val:
                        out, previews = val
                        self._show_previews(previews)
                        if out and messagebox.askyesno("완료", f"생성 완료:\n{out}\n\n결과 폴더를 열까요?"):
                            open_path(os.path.dirname(out))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _show_previews(self, paths):
        from PIL import Image, ImageTk
        for t in self.nb.tabs():
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


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
