"""tkinter GUI (Windows / macOS 공통).

흐름: [브이월드 3D 지도 열기] → 지도에서 구도 → [이 시점으로 생성]
      → 앱이 판단: 이미 있는 모델이면 카메라만 추가 / 없거나 새 데이터면 모자란 타일만 생성 후 조립
"""
import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

from .config import Settings, PRESETS, APP_NAME, APP_VERSION
from .util import Reporter, Cancelled
from . import blender_runner, library, vworld_api

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".kedd_citymodel.json")
DIM = "#8c8c8c"
NEED = "#d35400"


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
        self.minsize(1120, 720)
        self.s = Settings.load(SETTINGS_FILE) if os.path.exists(SETTINGS_FILE) else Settings()
        self.q = queue.Queue()
        self.busy = False
        self.cancel = False
        self.preview_imgs = []
        self._lib_rows = {}
        self.server = None
        self._init_view = dict(lon=127.0, lat=37.5, alt=9000, heading=0, tilt=-40)
        self._build()
        self._load_to_ui()
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(100, self._poll)
        self.after(600, lambda: self._fetch_catalog(silent=True))
        self.after(300, self._start_server)
        if not self.s.blender_path:
            b = blender_runner.find_blender()
            if b:
                self.v_blender.set(b)

    # ================================================================ UI
    def _build(self):
        pad = dict(padx=6, pady=3)
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        left = ttk.Frame(root, width=360)
        left.pack(side="left", fill="y")
        right = ttk.Frame(root)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        # --- 프로젝트 ---
        f = ttk.LabelFrame(left, text="프로젝트")
        f.pack(fill="x", **pad)
        self.v_proj = tk.StringVar()
        self.cb_proj = ttk.Combobox(f, textvariable=self.v_proj, width=28)
        self.cb_proj.pack(fill="x", **pad)
        ttk.Label(f, text="같은 프로젝트에 카메라가 계속 쌓입니다. 새 이름을 쓰면 새 파일.", foreground=DIM,
                  wraplength=330).pack(anchor="w", **pad)

        # --- 시점 ---
        f = ttk.LabelFrame(left, text="시점 → 생성")
        f.pack(fill="x", **pad)
        ttk.Button(f, text="🗺  브이월드 3D 지도 열기", command=self._open_map).pack(fill="x", **pad)
        self.lbl_map = ttk.Label(f, text="지도에서 구도를 잡고 [이 시점으로 생성]을 누르세요.", foreground=DIM, wraplength=330)
        self.lbl_map.pack(anchor="w", **pad)
        ttk.Separator(f).pack(fill="x", pady=4)
        ttk.Label(f, text="또는 웹 지도 URL (구글/구글어스/네이버/카카오)").pack(anchor="w", **pad)
        self.v_cam = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_cam).pack(fill="x", **pad)
        ttk.Button(f, text="URL 시점으로 생성", command=self._run_url).pack(fill="x", **pad)
        ttk.Separator(f).pack(fill="x", pady=4)
        rf = ttk.Frame(f)
        rf.pack(fill="x")
        self.v_preset = tk.StringVar(value="범위 프리셋…")
        cb = ttk.Combobox(rf, textvariable=self.v_preset, values=list(PRESETS), state="readonly", width=18)
        cb.pack(side="left", **pad)
        cb.bind("<<ComboboxSelected>>", self._on_preset)
        ttk.Button(rf, text="범위로 생성", command=self._run_bbox).pack(side="left", fill="x", expand=True, **pad)
        bb = ttk.Frame(f)
        bb.pack(fill="x")
        self.v_bbox = [tk.StringVar() for _ in range(4)]
        for i, lab in enumerate(["서", "남", "동", "북"]):
            ttk.Label(bb, text=lab).grid(row=0, column=i * 2, padx=(6, 1))
            ttk.Entry(bb, textvariable=self.v_bbox[i], width=8).grid(row=0, column=i * 2 + 1)
        for v in self.v_bbox:
            v.trace_add("write", lambda *a: self._refresh_lib() if self._lib_rows else None)

        # --- 옵션 ---
        f = ttk.LabelFrame(left, text="옵션")
        f.pack(fill="x", **pad)
        self.v_exag = tk.DoubleVar()
        self.v_ao = tk.DoubleVar()
        self.v_minor = tk.BooleanVar()
        self.v_api = tk.BooleanVar()
        self.v_prev = tk.BooleanVar()
        g = ttk.Frame(f)
        g.pack(fill="x")
        ttk.Label(g, text="지형 과장").grid(row=0, column=0, sticky="e", **pad)
        ttk.Spinbox(g, from_=1.0, to=4.0, increment=0.1, textvariable=self.v_exag, width=5).grid(row=0, column=1, sticky="w")
        ttk.Label(g, text="AO").grid(row=0, column=2, sticky="e", **pad)
        ttk.Spinbox(g, from_=0.0, to=1.0, increment=0.05, textvariable=self.v_ao, width=5).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(f, text="이면도로 포함", variable=self.v_minor).pack(anchor="w", **pad)
        ttk.Checkbutton(f, text="브이월드 API로 건물 높이 보완", variable=self.v_api).pack(anchor="w", **pad)
        ttk.Checkbutton(f, text="완료 후 미리보기 렌더", variable=self.v_prev).pack(anchor="w", **pad)
        cf = ttk.Frame(f)
        cf.pack(anchor="w", **pad)
        self.col_btns = {}
        for key, lab in [("col_green", "녹지"), ("col_water", "물"), ("col_road", "도로"), ("col_building", "건물")]:
            ttk.Label(cf, text=lab).pack(side="left")
            b = tk.Label(cf, width=3, relief="solid", bd=1, cursor="hand2", text=" ")
            b.pack(side="left", padx=(2, 8), ipady=2)
            b.bind("<Button-1>", lambda e, k=key: self._pick_color(k))
            self.col_btns[key] = b

        # --- 진행 ---
        f = ttk.Frame(left)
        f.pack(fill="x", **pad)
        self.pb = ttk.Progressbar(f, maximum=1000)
        self.pb.pack(fill="x", pady=2)
        bf = ttk.Frame(f)
        bf.pack(fill="x")
        self.btn_stop = ttk.Button(bf, text="중지", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(bf, text="Blender로 열기", command=self._open_blend).pack(side="left", padx=4)
        ttk.Button(bf, text="폴더", command=lambda: open_path(self._proj_dir())).pack(side="left")

        # --- 오른쪽 탭 ---
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.txt = tk.Text(right, height=11, wrap="word", font=("Menlo" if sys.platform == "darwin" else "Consolas", 10))
        self.txt.pack(fill="both", expand=False, pady=(6, 0))
        t_lib = ttk.Frame(self.nb, padding=6)
        t_set = ttk.Frame(self.nb, padding=6)
        self.nb.add(t_lib, text="건물 데이터")
        self.nb.add(t_set, text="설정")
        self._fixed_tabs = 2

        # 건물 데이터 탭
        top = ttk.Frame(t_lib)
        top.pack(fill="x")
        ttk.Label(top, text="폴더").pack(side="left")
        self.v_lib = tk.StringVar()
        ttk.Entry(top, textvariable=self.v_lib).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="…", width=3, command=self._pick_lib).pack(side="left")
        tf = ttk.Frame(t_lib)
        tf.pack(fill="both", expand=True, pady=6)
        self.tv = ttk.Treeview(tf, columns=("need", "name", "latest", "have", "size", "state"), show="headings",
                               height=16, selectmode="extended")
        for c, t, w in [("need", "범위", 44), ("name", "시도", 150), ("latest", "브이월드 최신", 100), ("have", "내 폴더", 100),
                        ("size", "용량", 70), ("state", "상태", 70)]:
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="center" if c != "name" else "w")
        self.tv.tag_configure("dim", foreground=DIM)
        self.tv.tag_configure("need", foreground=NEED)
        self.tv.pack(fill="both", expand=True)
        self.tv.bind("<<TreeviewSelect>>", self._on_tv_select)
        bot = ttk.Frame(t_lib)
        bot.pack(fill="x")
        ttk.Button(bot, text="선택 받기", command=self._download_selected).pack(side="left")
        self.lbl_lib = ttk.Label(bot, text="", foreground=DIM, wraplength=560)
        self.lbl_lib.pack(side="left", padx=8)

        # 설정 탭
        rows = [("작업 폴더", "v_work", self._pick_work), ("Blender", "v_blender", self._pick_blender)]
        for r, (lab, var, cmd) in enumerate(rows):
            setattr(self, var, tk.StringVar())
            ttk.Label(t_set, text=lab).grid(row=r, column=0, sticky="e", **pad)
            ttk.Entry(t_set, textvariable=getattr(self, var), width=60).grid(row=r, column=1, sticky="we", **pad)
            ttk.Button(t_set, text="…", width=3, command=cmd).grid(row=r, column=2, **pad)
        ttk.Label(t_set, text="브이월드 인증키").grid(row=2, column=0, sticky="e", **pad)
        self.v_key = tk.StringVar()
        ttk.Entry(t_set, textvariable=self.v_key, width=60, show="•").grid(row=2, column=1, sticky="we", **pad)
        ttk.Button(t_set, text="저장·확인", command=self._save_key).grid(row=2, column=2, **pad)
        ttk.Label(t_set, text="인증키 발급 시 서비스 URL 은 http://localhost:47380 으로 입력하세요. 키는 운영체제 키체인에 저장됩니다.",
                  foreground=DIM).grid(row=3, column=1, sticky="w", **pad)
        ttk.Button(t_set, text="고급 설정…", command=self._advanced).grid(row=4, column=1, sticky="w", **pad)

        self._log(f"{APP_NAME} {APP_VERSION}")

    # ================================================================ 설정 ↔ UI
    def _projects(self):
        d = os.path.join(self.v_work.get() or self.s.work_dir, "projects")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def _proj_dir(self):
        return os.path.join(self.v_work.get() or self.s.work_dir, "projects", self.v_proj.get().strip() or "seoul")

    def _load_to_ui(self):
        s = self.s
        for v, x in zip(self.v_bbox, s.bbox):
            v.set(f"{x:.4f}")
        self.v_lib.set(s.building_lib)
        self.v_work.set(s.work_dir)
        self.v_blender.set(s.blender_path)
        self.v_proj.set(s.project_name)
        self.cb_proj["values"] = self._projects()
        self.v_cam.set(s.camera_url)
        self.v_exag.set(s.exag)
        self.v_ao.set(s.ao_strength)
        self.v_minor.set(s.include_minor_roads)
        self.v_api.set(s.api_heights)
        self.v_prev.set(s.preview_render)
        self.v_key.set(vworld_api.get_key())
        for k, b in self.col_btns.items():
            b.config(bg=_lin2hex(getattr(s, k)))
        self._refresh_lib()

    def _ui_to_settings(self):
        s = self.s
        try:
            s.lon_min, s.lat_min, s.lon_max, s.lat_max = [float(v.get()) for v in self.v_bbox]
        except ValueError:
            pass
        s.building_lib = self._lib()
        s.building_zips = []
        s.work_dir = self.v_work.get().strip()
        s.project_name = "".join(ch for ch in self.v_proj.get().strip() if ch not in '\\/:*?"<>|') or "seoul"
        s.blender_path = self.v_blender.get().strip()
        s.camera_url = self.v_cam.get().strip()
        s.exag = float(self.v_exag.get())
        s.ao_strength = float(self.v_ao.get())
        s.include_minor_roads = bool(self.v_minor.get())
        s.api_heights = bool(self.v_api.get())
        s.preview_render = bool(self.v_prev.get())
        try:
            s.save(SETTINGS_FILE)
        except Exception:
            pass
        return s

    # ================================================================ 건물 데이터
    def _lib(self):
        return self.v_lib.get().strip() or self.s.building_lib

    def _cur_bbox(self):
        try:
            return tuple(float(v.get()) for v in self.v_bbox)
        except Exception:
            return None

    def _refresh_lib(self):
        sel = set(self.tv.selection())
        for i in self.tv.get_children():
            self.tv.delete(i)
        rows, fetched = library.overview(self._lib(), self._cur_bbox())
        self._lib_rows = {r["code"]: r for r in rows}
        for r in rows:
            have = r["state"] == "보유"
            tag = "dim" if have else ("need" if r["need"] else "")
            st = "보유" if have else "없음"
            if r["state"] == "새 버전":
                st = "보유(구)"
            self.tv.insert("", "end", iid=r["code"], tags=(tag,) if tag else (),
                           values=("★" if r["need"] else "", r["name"], r["latest"], r["have"], r["size"], st))
        for i in sel:
            if self.tv.exists(i) and self._lib_rows[i]["state"] != "보유":
                self.tv.selection_add(i)
        need = [r["name"] for r in rows if r["need"] and r["state"] != "보유"]
        msg = (f"브이월드 목록 {fetched[:10]} 기준 · " if fetched else "브이월드 목록 확인 중 · ")
        msg += (f"현재 범위에 필요한데 없는 데이터: {', '.join(need)}" if need else "현재 범위의 데이터는 모두 있습니다")
        self.lbl_lib.config(text=msg)

    def _on_tv_select(self, *_):
        for i in self.tv.selection():
            if self._lib_rows.get(i, {}).get("state") == "보유":
                self.tv.selection_remove(i)

    def _fetch_catalog(self, silent=False):
        def work():
            try:
                library.fetch_catalog(self._lib())
            except Exception as e:
                if not silent:
                    self.q.put(("log", f"브이월드 목록을 불러오지 못했습니다: {e}"))
            self.q.put(("refresh_lib", None))
        threading.Thread(target=work, daemon=True).start()

    def _download_selected(self):
        rows = [self._lib_rows[i] for i in self.tv.selection() if self._lib_rows[i]["state"] != "보유"]
        if not rows:
            rows = [r for r in self._lib_rows.values() if r["need"] and r["state"] != "보유"]
        if not rows:
            messagebox.showinfo("받기", "받을 항목이 없습니다. 목록에서 '없음' 항목을 선택하세요.")
            return
        if any(r["latest"] == "?" for r in rows):
            messagebox.showwarning("받기", "브이월드 목록을 아직 불러오지 못했습니다. 잠시 후 다시 시도하세요.")
            self._fetch_catalog()
            return
        total = sum(int((r["size"] or "0MB").rstrip("MB") or 0) for r in rows)
        if not messagebox.askyesno("받기", "다음 데이터를 받아 건물 데이터 폴더에 넣습니다.\n"
                                         "(브라우저에서 브이월드에 로그인되어 있어야 합니다)\n\n"
                                         + "\n".join(f"· {r['name']} {r['latest']} ({r['size']})" for r in rows)
                                         + f"\n\n합계 약 {total:,}MB"):
            return
        for r in rows:
            webbrowser.open(r["url"])
        self._log("브라우저에서 다운로드를 시작했습니다. 받기가 끝나면 자동으로 폴더로 옮기고 인덱스를 만듭니다.")
        codes = [r["code"] for r in rows]
        threading.Thread(target=library.download_watch,
                         args=(self._lib(), codes, lambda m: self.q.put(("log", m)), lambda: self.q.put(("refresh_lib", None))),
                         daemon=True).start()

    def _pick_lib(self):
        d = filedialog.askdirectory(initialdir=self._lib())
        if d:
            self.v_lib.set(d)
            self._refresh_lib()

    # ================================================================ 지도 / 시점
    def _start_server(self):
        from .mapserver import MapServer
        try:
            self.server = MapServer(vworld_api.get_key, self._on_capture, self._map_init).start()
        except OSError:
            self._log("⚠ 지도 서버 포트(47380)를 열 수 없습니다. 앱이 이미 실행 중인지 확인하세요.")

    def _map_init(self):
        return self._init_view

    def _calc_init(self):
        b = self._cur_bbox() or (126.9, 37.45, 127.1, 37.6)
        self._init_view = dict(lon=(b[0] + b[2]) / 2, lat=(b[1] + b[3]) / 2 - 0.03, alt=9000, heading=0, tilt=-40)

    def _open_map(self):
        if not vworld_api.get_key():
            messagebox.showinfo("브이월드", "설정 탭에서 브이월드 인증키를 먼저 저장하세요.")
            self.nb.select(1)
            return
        self._calc_init()
        if not self.server:
            self._start_server()
        if not self.server:
            return
        webbrowser.open(self.server.url())

    def _on_capture(self, cam):
        """지도 페이지에서 시점 수신 (서버 스레드)"""
        if self.busy:
            return "앱이 다른 작업 중입니다. 끝난 뒤 다시 보내 주세요."
        self.q.put(("capture", cam))
        return "앱으로 보냈습니다. 앱 창에서 진행 여부를 확인하세요."

    def _run_url(self):
        from .pipeline import url_camera
        try:
            cam = url_camera(self.s, self.v_cam.get())
        except Exception as e:
            messagebox.showerror("URL", f"URL 을 해석하지 못했습니다: {e}")
            return
        self._start_job([cam], f"{cam['source']} 시점")

    def _run_bbox(self):
        from .pipeline import bbox_camera
        s = self._ui_to_settings()
        self._start_job([bbox_camera(s)], "범위 전체")

    def _on_preset(self, *_):
        p = PRESETS.get(self.v_preset.get())
        if p:
            for v, x in zip(self.v_bbox, p):
                v.set(f"{x:.4f}")

    # ================================================================ 작업
    def _start_job(self, cams, label):
        if self.busy:
            messagebox.showinfo("작업 중", "다른 작업이 진행 중입니다.")
            return
        s = self._ui_to_settings()
        if s.blender_path and not os.path.isfile(s.blender_path):
            messagebox.showerror("Blender", "설정 탭의 Blender 경로가 올바르지 않습니다.")
            return
        self.busy = True
        self.lbl_map.config(text=f"'{label}' 계획 확인 중…")

        def plan_then_run():
            from .pipeline import plan, run_job
            try:
                p = plan(s, s.project_name, cams)
            except Exception as e:
                self.q.put(("log", "✖ " + str(e)))
                self.q.put(("done", None))
                return
            self.q.put(("confirm", (p, cams, label)))
        threading.Thread(target=plan_then_run, daemon=True).start()

    def _confirm_and_run(self, p, cams, label):
        s = self.s
        n_need, n_build = len(p["needed"]), len(p["build"])
        if p["camera_only"]:
            text = f"[{s.project_name}] 이미 만든 모델이 이 시점을 덮고 있습니다.\n카메라만 추가합니다 (수 초)."
        else:
            have = n_need - n_build
            est = n_build * 0.9 + 1.5
            text = (f"[{s.project_name}] {label}\n\n필요한 4km 타일 {n_need}개 중\n"
                    f"· 이미 있음 {have}개 (재사용)\n· 새로/다시 만들 타일 {n_build}개\n\n"
                    f"예상 약 {est:.0f}분 후 .blend 를 다시 조립하고 카메라를 추가합니다.")
            if p["exag_changed"]:
                text += "\n\n⚠ 지형 과장이 바뀌어 기존 타일도 모두 다시 만듭니다."
        if not messagebox.askyesno("진행할까요?", text):
            self.busy = False
            self.lbl_map.config(text="취소했습니다.")
            return
        self.cancel = False
        self.btn_stop.config(state="normal")
        self.pb["value"] = 0
        self._log("─" * 40)

        def work():
            from .pipeline import run_job
            rep = Reporter(log=lambda m: self.q.put(("log", m)), progress=lambda v: self.q.put(("prog", v)),
                           cancel_flag=lambda: self.cancel)
            try:
                out, how = run_job(s, s.project_name, cams, rep, cancel=lambda: self.cancel)
                self.q.put(("done", (out, how)))
            except Cancelled:
                self.q.put(("log", "■ 중지됨"))
                self.q.put(("done", None))
            except Exception as e:
                self.q.put(("log", "✖ 오류: " + str(e)))
                self.q.put(("log", traceback.format_exc()))
                self.q.put(("done", None))
        threading.Thread(target=work, daemon=True).start()

    def _quit(self):
        if self.busy and not messagebox.askyesno("종료", "작업이 진행 중입니다. 중지하고 종료할까요?"):
            return
        self.cancel = True
        if self.server:
            self.server.stop()
        self.destroy()

    def _stop(self):
        self.cancel = True
        self._log("중지 요청…")

    def _open_blend(self):
        p = os.path.join(self._proj_dir(), f"{os.path.basename(self._proj_dir())}.blend")
        if not os.path.exists(p):
            messagebox.showinfo("Blender", "아직 이 프로젝트의 .blend 가 없습니다.")
            return
        b = self.v_blender.get().strip() or blender_runner.find_blender()
        if b:
            subprocess.Popen([b, p])
        else:
            open_path(p)

    # ================================================================ 기타
    def _pick_work(self):
        d = filedialog.askdirectory(initialdir=self.v_work.get() or os.path.expanduser("~"))
        if d:
            self.v_work.set(d)
            self.cb_proj["values"] = self._projects()

    def _pick_blender(self):
        ft = [("Blender", "blender.exe")] if sys.platform.startswith("win") else [("모든 파일", "*")]
        f = filedialog.askopenfilename(title="Blender 실행 파일 (Mac: Blender.app)", filetypes=ft,
                                       initialdir="/Applications" if sys.platform == "darwin" else None)
        if f:
            if f.endswith(".app"):
                f = os.path.join(f, "Contents", "MacOS", "Blender")
            self.v_blender.set(f)
            self._log(f"Blender 버전: {blender_runner.blender_version(f) or '확인 실패'}")

    def _save_key(self):
        k = self.v_key.get().strip()
        where = vworld_api.set_key(k)
        if not k:
            self._log("인증키를 지웠습니다.")
            return

        def work():
            ok, m = vworld_api.test_key(k)
            self.q.put(("log", ("✔ " if ok else "✖ ") + m + f" (저장 위치: {'키체인' if where == 'keychain' else '사용자 폴더'})"))
        threading.Thread(target=work, daemon=True).start()

    def _pick_color(self, key):
        r = colorchooser.askcolor(color=_lin2hex(getattr(self.s, key)), title="색 선택")
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
                ("osm_max_age_days", "OSM 자동 갱신 주기 (일)", "int"),
                ("min_building_area", "최소 건물 면적 (m²)", "num"), ("default_height", "높이 없는 건물 기본 높이 (m)", "num"),
                ("road_offset", "도로 판 높이 (m)", "num"), ("ao_distance", "AO 거리 (m)", "num"),
                ("render_w", "렌더 가로 (px)", "int"), ("render_h", "렌더 세로 (px)", "int"), ("samples", "렌더 샘플", "int")]
        for r, (k, lab, typ) in enumerate(rows):
            ttk.Label(w, text=lab).grid(row=r, column=0, sticky="e", **pad)
            val = getattr(self.s, k)
            v = tk.StringVar(value=";".join(val) if isinstance(val, list) else str(val))
            vars_[k] = (v, typ)
            ttk.Entry(w, textvariable=v, width=40).grid(row=r, column=1, **pad)

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

    def _log(self, msg):
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._log(val)
                elif kind == "prog":
                    self.pb["value"] = val * 1000
                elif kind == "refresh_lib":
                    self._refresh_lib()
                elif kind == "capture":
                    self.lift()
                    cam = dict(val)
                    self._log(f"지도 시점 수신: 위도 {cam['lat']:.5f}, 경도 {cam['lon']:.5f}, 높이 {cam['height']:.0f}m, "
                              f"방위 {cam['heading']:.0f}°, 기울기 {90 + cam['pitch']:.0f}°")
                    self._start_job([cam], "브이월드 지도 시점")
                elif kind == "confirm":
                    self._confirm_and_run(*val)
                elif kind == "done":
                    self.busy = False
                    self.btn_stop.config(state="disabled")
                    self.cb_proj["values"] = self._projects()
                    if val:
                        out, how = val
                        self.lbl_map.config(text="카메라를 추가했습니다." if how == "camera" else "생성을 마쳤습니다.")
                        pv = os.path.join(self._proj_dir(), "preview.png")
                        if os.path.exists(pv) and how != "camera":
                            self._show_previews([pv])
                        if out and messagebox.askyesno("완료", f"{out}\n\nBlender 로 열까요?"):
                            self._open_blend()
                    else:
                        self.lbl_map.config(text="지도에서 구도를 잡고 [이 시점으로 생성]을 누르세요.")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _show_previews(self, paths):
        from PIL import Image, ImageTk
        for t in self.nb.tabs()[self._fixed_tabs:]:
            self.nb.forget(t)
        self.preview_imgs = []
        self.update_idletasks()
        W = max(400, self.nb.winfo_width() - 10)
        H = max(300, self.nb.winfo_height() - 30)
        for p in paths:
            im = Image.open(p)
            im.thumbnail((W, H))
            ph = ImageTk.PhotoImage(im)
            self.preview_imgs.append(ph)
            self.nb.add(ttk.Label(self.nb, image=ph), text="미리보기")
        if paths:
            self.nb.select(self._fixed_tabs)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
