"""OSM: Geofabrik 한국 pbf 다운로드 → 범위 안의 도로/다리, 건물, 녹지/물 영역을 한 번에 추출."""
import hashlib
import os
import pickle
import osmium
from shapely import wkb
from .util import download, Reporter

GEOFABRIK = "https://download.geofabrik.de/asia/south-korea-latest.osm.pbf"

KEEP_HW = {'motorway', 'motorway_link', 'trunk', 'trunk_link', 'primary', 'primary_link', 'secondary',
           'secondary_link', 'tertiary', 'tertiary_link', 'residential', 'unclassified', 'living_street'}
GREEN_L = {'forest', 'grass', 'meadow', 'recreation_ground', 'village_green', 'cemetery', 'greenfield',
           'orchard', 'plant_nursery'}
GREEN_LE = {'park', 'garden', 'golf_course', 'nature_reserve', 'common'}
GREEN_N = {'wood', 'scrub', 'grassland', 'heath'}
WATER_L = {'reservoir', 'basin'}


def _num(v):
    try:
        return float(str(v).replace('m', '').split(';')[0])
    except Exception:
        return None


class _Handler(osmium.SimpleHandler):
    def __init__(self, bbox, rep):
        super().__init__()
        self.b = (bbox[0] - 0.02, bbox[1] - 0.02, bbox[2] + 0.02, bbox[3] + 0.02)
        self.rep = rep
        self.fab = osmium.geom.WKBFactory()
        self.ways, self.buildings, self.green, self.water = [], [], [], []
        self.cnt = 0

    def _in(self, x, y):
        b = self.b
        return b[0] < x < b[2] and b[1] < y < b[3]

    def way(self, w):
        self.cnt += 1
        if self.cnt % 2_000_000 == 0:
            self.rep.progress(min(0.95, self.cnt / 40_000_000))
        t = w.tags
        hw = t.get('highway')
        rw = t.get('railway')
        br = t.get('bridge')
        if not ((hw in KEEP_HW) or (rw in ('rail', 'subway', 'light_rail') and br and br != 'no')):
            return
        if t.get('tunnel') not in (None, 'no') or t.get('area') == 'yes':
            return
        try:
            pts = [(n.lon, n.lat) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if not any(self._in(x, y) for x, y in pts):
            return
        self.ways.append(dict(hw=hw or 'rail', bridge=(br not in (None, 'no')), layer=t.get('layer'),
                              name=t.get('name', ''), lanes=t.get('lanes'), pts=pts))

    def area(self, a):
        t = a.tags
        isb = ('building' in t and t.get('building') != 'no')
        lu, le, na = t.get('landuse'), t.get('leisure'), t.get('natural')
        isw = na == 'water' or t.get('waterway') == 'riverbank' or lu in WATER_L or ('water' in t and na != 'wetland')
        isg = (lu in GREEN_L) or (le in GREEN_LE) or (na in GREEN_N)
        if not (isb or isw or isg):
            return
        try:
            g = wkb.loads(self.fab.create_multipolygon(a), hex=True)
        except Exception:
            return
        x0, y0, x1, y1 = g.bounds
        b = self.b
        if x1 < b[0] or x0 > b[2] or y1 < b[1] or y0 > b[3]:
            return
        if isb:
            self.buildings.append(dict(g=g.wkb, levels=_num(t.get('building:levels')), height=_num(t.get('height')),
                                       name=t.get('name', '')))
        elif isw:
            self.water.append(g.wkb)
        else:
            self.green.append(g.wkb)


def get_osm(s, rep: Reporter):
    """반환: dict(ways, buildings, green, water)  (WGS84)"""
    key = hashlib.md5(("%.4f,%.4f,%.4f,%.4f|" % s.bbox + (s.osm_pbf or "auto")).encode()).hexdigest()[:10]
    cache = os.path.join(s.cache_dir, "osm", f"extract_{key}.pkl")
    pbf = s.osm_pbf
    if not pbf:
        pbf = os.path.join(s.cache_dir, "osm", "south-korea-latest.osm.pbf")
        if not os.path.exists(pbf):
            rep.log("   OSM 한국 데이터 다운로드 (약 300MB, 최초 1회)")
        download(GEOFABRIK, pbf, rep, label="OSM")
    if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(pbf):
        rep.log("   (캐시된 OSM 추출 결과 사용)")
        return pickle.load(open(cache, "rb"))
    rep.log("   OSM 에서 범위 안의 도로·건물·녹지·물 추출 중 (1~3분)")
    h = _Handler(s.bbox, rep)
    h.apply_file(pbf, locations=True, idx='flex_mem')
    d = dict(ways=h.ways, buildings=h.buildings, green=h.green, water=h.water)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    pickle.dump(d, open(cache, "wb"))
    rep.log(f"   도로 {len(h.ways):,} · 건물 {len(h.buildings):,} · 녹지 {len(h.green):,} · 물 {len(h.water):,}")
    rep.progress(1.0)
    return d
