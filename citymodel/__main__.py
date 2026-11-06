"""실행: GUI (기본) 또는  python -m citymodel --config settings.json"""
import argparse
import sys


def main():
    ap = argparse.ArgumentParser(prog="citymodel")
    ap.add_argument("--config", help="settings.json 경로 (지정하면 GUI 없이 실행)")
    ap.add_argument("--vworld-login", help=argparse.SUPPRESS)
    a, _ = ap.parse_known_args()
    if a.vworld_login:
        from .vworld_login import login_main
        sys.exit(login_main(a.vworld_login))
    if a.config:
        from .config import Settings
        from .pipeline import run_job, bbox_camera, url_camera
        from .util import Reporter
        s = Settings.load(a.config)
        errs = s.validate()
        if errs:
            print("\n".join(errs))
            sys.exit(1)
        cam = url_camera(s, s.camera_url) if s.camera_url.strip() else bbox_camera(s)
        out, how = run_job(s, s.project_name, [cam], Reporter(progress=lambda p: None))
        print(f"{how}: {out}")
    else:
        from .gui import main as gui_main
        gui_main()


if __name__ == "__main__":
    main()
