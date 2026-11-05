"""건물 데이터 라이브러리: 전국 시도별 GIS건물통합정보 zip 을 한 폴더에 두고 관리.

폴더 구조
  <lib>/AL_D010_11_20261009.zip        ← 브이월드에서 받은 원본 (시도별, 여러 날짜 공존 가능)
  <lib>/_index/11_20261009/            ← 자동 생성 인덱스 (빠른 범위 검색용)
        meta.json, coords.npy, ring_start.npy, ring_len.npy, ring_bbox.npy, ring_h.npy, ring_d.npy

시도마다 날짜가 가장 최근인 zip 만 사용하며, 새 zip 을 넣으면 다음 실행 때 인덱스를 다시 만든다.
"""
import datetime as dt
import glob
import json
import os
import re
import shutil
import tempfile
import zipfile
import numpy as np
import shapefile
from pyproj import CRS, Transformer
from .util import Reporter, ring_area

SIDO = {
    "11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시", "29": "광주광역시",
    "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시", "41": "경기도", "42": "강원도",
    "43": "충청북도", "44": "충청남도", "45": "전라북도", "46": "전라남도", "47": "경상북도",
    "48": "경상남도", "50": "제주특별자치도", "51": "강원특별자치도", "52": "전북특별자치도",
}
FNAME = re.compile(r"AL_D010_(\d{2})_(\d{8})\.zip$", re.I)
VWORLD_PAGE = "https://www.data.go.kr/data/15083092/fileData.do"   # 브이월드 GIS건물통합정보 다운로드로 연결
STALE_DAYS = 90
IDX_VERSION = 2


def default_lib_dir():
    return os.path.join(os.path.expanduser("~"), "CityModel", "건물데이터")


def scan(lib):
    """→ {code: {"zip", "date", "name", "older": [...]}} (시도별 최신)"""
    groups = {}
    for p in glob.glob(os.path.join(lib, "*.zip")):
        m = FNAME.search(os.path.basename(p))
        if m:
            groups.setdefault(m.group(1), []).append((m.group(2), p))
    out = {}
    for code, lst in groups.items():
        lst.sort(reverse=True)
        out[code] = dict(zip=lst[0][1], date=lst[0][0], name=SIDO.get(code, code), older=[p for _, p in lst[1:]])
    return out


def index_dir(lib, code, date):
    return os.path.join(lib, "_index", f"{code}_{date}")


def read_meta(lib, code, date):
    p = os.path.join(index_dir(lib, code, date), "meta.json")
    if os.path.exists(p):
        m = json.load(open(p, encoding="utf-8"))
        if m.get("version") == IDX_VERSION:
            return m
    return None


def status(lib):
    """GUI 표시용 목록"""
    rows = []
    today = dt.date.today()
    for code, e in sorted(scan(lib).items()):
        d = dt.datetime.strptime(e["date"], "%Y%m%d").date()
        meta = read_meta(lib, code, e["date"])
        age = (today - d).days
        rows.append(dict(code=code, name=e["name"], date=d.isoformat(), age=age,
                         count=meta["count"] if meta else None, indexed=bool(meta),
                         stale=age > STALE_DAYS, zip=e["zip"]))
    return rows


def import_files(lib, paths, move=False):
    """zip 들을 라이브러리로 복사/이동. 이름 형식이 맞는 것만. → 가져온 파일 목록"""
    os.makedirs(lib, exist_ok=True)
    done = []
    for p in paths:
        b = os.path.basename(p)
        if not FNAME.search(b):
            continue
        dest = os.path.join(lib, b)
        if os.path.abspath(p) == os.path.abspath(dest) or os.path.exists(dest):
            continue
        (shutil.move if move else shutil.copy2)(p, dest)
        done.append(dest)
    return done


def find_in_downloads(lib):
    """다운로드 폴더에서 라이브러리보다 새로운 AL_D010 zip 찾기"""
    cur = scan(lib) if os.path.isdir(lib) else {}
    dl = os.path.join(os.path.expanduser("~"), "Downloads")
    found = []
    for p in glob.glob(os.path.join(dl, "**", "AL_D010_*.zip"), recursive=True):
        m = FNAME.search(os.path.basename(p))
        if not m:
            continue
        code, date = m.group(1), m.group(2)
        if code not in cur or date > cur[code]["date"]:
            found.append(p)
    return found


def remove_old(lib):
    """시도별 최신 외의 zip 과 쓰지 않는 인덱스 삭제 → 삭제한 파일 수"""
    n = 0
    sc = scan(lib)
    keep = {f"{c}_{e['date']}" for c, e in sc.items()}
    for e in sc.values():
        for p in e["older"]:
            os.remove(p)
            n += 1
    for d in glob.glob(os.path.join(lib, "_index", "*")):
        if os.path.basename(d) not in keep:
            shutil.rmtree(d, ignore_errors=True)
    return n


# ---------------------------------------------------------------- 인덱스
def _field(names, *cands):
    for c in cands:
        if c in names:
            return names.index(c)
    return None


def build_index(lib, code, date, zpath, rep: Reporter):
    from .buildings import gis_height
    out = index_dir(lib, code, date)
    tmp = tempfile.mkdtemp(prefix="gis_", dir=lib)
    try:
        rep.log(f"   {SIDO.get(code, code)} {date} 인덱스 생성 (최초 1회)")
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist() if not n.startswith("__MACOSX") and not os.path.basename(n).startswith("._")]
            z.extractall(tmp, members=names)
        shps = glob.glob(os.path.join(tmp, "**", "*.shp"), recursive=True)
        if not shps:
            raise RuntimeError(f"{os.path.basename(zpath)} 안에 .shp 가 없습니다")
        coords, rs, rl, rb, rh, rd = [], [], [], [], [], []
        npts = 0
        nb = 0
        for shp in shps:
            base = os.path.splitext(shp)[0]
            enc = "cp949"
            if os.path.exists(base + ".cst"):
                e = open(base + ".cst", encoding="ascii", errors="ignore").read().strip().lower()
                enc = "cp949" if e in ("euc-kr", "cp949", "ks_c_5601-1987", "") else e
            tr = None
            if os.path.exists(base + ".prj"):
                try:
                    c = CRS.from_wkt(open(base + ".prj", encoding="utf-8", errors="ignore").read())
                    if c.to_epsg() != 5186:
                        tr = Transformer.from_crs(c, 5186, always_xy=True)
                except Exception:
                    pass
            r = shapefile.Reader(shp, encoding=enc, encodingErrors="replace")
            fn = [f[0] for f in r.fields[1:]]
            ih, iff = _field(fn, "A16"), _field(fn, "A26")
            N = len(r)
            for i, sr in enumerate(r.iterShapeRecords()):
                if i % 50000 == 0:
                    rep.progress(i / max(N, 1))
                sh = sr.shape
                if not sh.points:
                    continue
                rec = sr.record
                h, isd = gis_height(rec[ih] if ih is not None else 0, rec[iff] if iff is not None else 0, -1.0)
                pts = np.asarray(sh.points, float)
                if tr is not None:
                    pts = np.c_[tr.transform(pts[:, 0], pts[:, 1])]
                parts = list(sh.parts) + [len(pts)]
                for p in range(len(parts) - 1):
                    rr = pts[parts[p]:parts[p + 1]]
                    if len(rr) < 4 or ring_area(rr) > 0:        # 외곽(시계방향)만
                        continue
                    coords.append(rr)
                    rs.append(npts)
                    rl.append(len(rr))
                    rb.append((rr[:, 0].min(), rr[:, 1].min(), rr[:, 0].max(), rr[:, 1].max()))
                    rh.append(h if not isd else -1.0)
                    rd.append(nb)
                    npts += len(rr)
                nb += 1
        os.makedirs(out, exist_ok=True)
        np.save(os.path.join(out, "coords.npy"), np.vstack(coords) if coords else np.zeros((0, 2)))
        np.save(os.path.join(out, "ring_start.npy"), np.array(rs, np.int64))
        np.save(os.path.join(out, "ring_len.npy"), np.array(rl, np.int32))
        rb = np.array(rb, np.float64).reshape(-1, 4)
        np.save(os.path.join(out, "ring_bbox.npy"), rb)
        np.save(os.path.join(out, "ring_h.npy"), np.array(rh, np.float32))
        np.save(os.path.join(out, "ring_b.npy"), np.array(rd, np.int32))
        bounds = [float(rb[:, 0].min()), float(rb[:, 1].min()), float(rb[:, 2].max()), float(rb[:, 3].max())] if len(rb) else [0, 0, 0, 0]
        json.dump(dict(version=IDX_VERSION, code=code, date=date, name=SIDO.get(code, code), count=nb,
                       rings=len(rl), bounds=bounds, source=os.path.basename(zpath)),
                  open(os.path.join(out, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False)
        rep.log(f"   → {nb:,}동 인덱스 완료")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rep.progress(1.0)


def ensure_indexes(lib, rep: Reporter):
    sc = scan(lib)
    todo = [(c, e) for c, e in sc.items() if not read_meta(lib, c, e["date"])]
    for k, (c, e) in enumerate(todo):
        build_index(lib, c, e["date"], e["zip"], rep)
    return sc


def query(lib, gbbox, rep: Reporter):
    """전역 TM bbox 와 겹치는 외곽 링들을 반환: list of (ring ndarray, height(-1=없음))"""
    sc = ensure_indexes(lib, rep)
    res = []
    used = []
    for c, e in sorted(sc.items()):
        meta = read_meta(lib, c, e["date"])
        b = meta["bounds"]
        if b[2] < gbbox[0] or b[0] > gbbox[2] or b[3] < gbbox[1] or b[1] > gbbox[3]:
            continue
        d = index_dir(lib, c, e["date"])
        bb = np.load(os.path.join(d, "ring_bbox.npy"))
        sel = np.where((bb[:, 2] >= gbbox[0]) & (bb[:, 0] <= gbbox[2]) & (bb[:, 3] >= gbbox[1]) & (bb[:, 1] <= gbbox[3]))[0]
        if not len(sel):
            continue
        coords = np.load(os.path.join(d, "coords.npy"), mmap_mode="r")
        st = np.load(os.path.join(d, "ring_start.npy"))[sel]
        ln = np.load(os.path.join(d, "ring_len.npy"))[sel]
        hh = np.load(os.path.join(d, "ring_h.npy"))[sel]
        for s0, l0, h0 in zip(st, ln, hh):
            res.append((np.asarray(coords[s0:s0 + l0]), float(h0)))
        used.append(f"{meta['name']}({meta['date']}) {len(sel):,}")
    rep.log("   사용한 건물 데이터: " + (", ".join(used) if used else "없음"))
    return res
