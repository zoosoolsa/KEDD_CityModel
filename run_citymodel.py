# PyInstaller 진입점
from citymodel.__main__ import main
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
