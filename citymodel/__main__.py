"""실행: GUI (기본) 또는  python -m citymodel --config settings.json"""
import argparse
import sys


def main():
    ap = argparse.ArgumentParser(prog="citymodel")
    ap.add_argument("--config", help="settings.json 경로 (지정하면 GUI 없이 실행)")
    ap.add_argument("--no-blender", action="store_true", help="Blender 단계 생략")
    a, _ = ap.parse_known_args()
    if a.config:
        from .config import Settings
        from .pipeline import run
        from .util import Reporter
        s = Settings.load(a.config)
        if a.no_blender:
            s.blender_path = "__none__"
        errs = s.validate()
        if errs:
            print("\n".join(errs))
            sys.exit(1)
        run(s, Reporter(progress=lambda p: None))
    else:
        from .gui import main as gui_main
        gui_main()


if __name__ == "__main__":
    main()
