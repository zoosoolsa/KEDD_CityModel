"""지면 텍스처 (타일별 RGBA PNG): R=녹지, G=물, B=주요 도로, A=이면도로.

도로를 지면에 직접 칠하므로 도로와 지면 사이 높이 차이가 생기지 않는다.
(다리·고가는 roads.py 에서 입체로 만든다)
"""
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as nd
from shapely import wkb
from .util import to_tm, Reporter

Image.MAX_IMAGE_PIXELS = None
SS = 2          # 슈퍼샘플링 배수 (가장자리 부드럽게)


class LandcoverSource:
    """한 영역의 OSM 녹지·물·도로를 TM 좌표로 한 번만 변환해 두고, 타일마다 잘라 그린다."""

    def __init__(self, s, osm, rep: Reporter):
        from .roads import MAJOR, _width
        self.s = s

        def polys(geoms):
            out = []
            for gb in geoms:
                g = wkb.loads(gb)
                for pg in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
                    rings = []
                    for ring in [pg.exterior] + list(pg.interiors):
                        lon, lat = np.asarray(ring.coords).T[:2]
                        X, Y = to_tm(lon, lat)
                        rings.append(np.c_[X, Y])
                    e = rings[0]
                    out.append((e[:, 0].min(), e[:, 1].min(), e[:, 0].max(), e[:, 1].max(), rings))
            return out

        self.green = polys(osm["green"])
        rep.progress(0.3)
        self.water = polys(osm["water"])
        rep.progress(0.5)
        self.major, self.minor = [], []
        for w in osm["ways"]:
            if w["bridge"]:
                continue
            lon, lat = np.array(w["pts"]).T
            X, Y = to_tm(lon, lat)
            p = np.c_[X, Y]
            if len(p) < 2:
                continue
            item = (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max(), p, _width(w))
            (self.major if w["hw"] in MAJOR else self.minor).append(item)
        rep.progress(1.0)

    @staticmethod
    def _hit(it, b, m):
        return not (it[2] < b[0] - m or it[0] > b[2] + m or it[3] < b[1] - m or it[1] > b[3] + m)

    def render_tile(self, bounds, terrain, res):
        """bounds=(x0,y0,x1,y1) 전역 TM → RGBA uint8 (위쪽이 북)"""
        x0, y0, x1, y1 = bounds
        w, h = int(round((x1 - x0) / res)), int(round((y1 - y0) / res))
        rs = res / SS
        W, H = w * SS, h * SS

        def px(a):
            return list(zip(((a[:, 0] - x0) / rs).tolist(), ((y1 - a[:, 1]) / rs).tolist()))

        def fill(items):
            im = Image.new("L", (W, H), 0)
            dr = ImageDraw.Draw(im)
            for it in items:
                if self._hit(it, bounds, 0):
                    for i, ring in enumerate(it[4]):
                        if len(ring) >= 3:
                            dr.polygon(px(ring), fill=255 if i == 0 else 0)
            return im

        def lines(items):
            im = Image.new("L", (W, H), 0)
            dr = ImageDraw.Draw(im)
            for it in items:
                if self._hit(it, bounds, 40):
                    wd = max(1, int(round(it[5] / rs)))
                    pts = px(it[4])
                    dr.line(pts, fill=255, width=wd, joint="curve")
                    r = wd / 2                       # 끝 마감 (둥글게)
                    for (a, b) in (pts[0], pts[-1]):
                        dr.ellipse((a - r, b - r, a + r, b + r), fill=255)
            return im

        def down(im):
            return np.asarray(im.resize((w, h), Image.BOX))

        green = down(fill(self.green))
        water = down(fill(self.water)).astype(np.float32)
        # 지형에서 찾은 물 (한강·바다 등)
        cx, cy = terrain.origin
        gx = x0 - cx + (np.arange(w) + 0.5) * res
        gy = y1 - cy - (np.arange(h) + 0.5) * res
        fx = np.clip(np.round((gx - terrain.x0) / terrain.res).astype(int), 0, terrain.W - 1)
        fy = np.clip(np.round((terrain.y0 - gy) / terrain.res).astype(int), 0, terrain.H - 1)
        tw = terrain.water.astype(bool)[fy][:, fx]
        tw = nd.binary_erosion(tw, iterations=max(1, int(round(5 / res))))
        tw = nd.gaussian_filter(tw.astype(np.float32), 1.2 * 5 / res) * 255
        water = np.maximum(water, tw)
        green = np.minimum(green.astype(np.float32), 255 - water)
        major = down(lines(self.major)).astype(np.float32)
        minor = down(lines(self.minor)).astype(np.float32)
        major = np.minimum(major, 255 - water)      # 물 위의 도로(다리 아닌 오류 구간)는 그리지 않음
        minor = np.minimum(minor, 255 - water)
        img = np.dstack([green, water, major, minor]).clip(0, 255).astype(np.uint8)
        return img


def preview_png(lc_png, path, s, width=1000):
    im = Image.open(lc_png).convert("RGBA")
    im = im.resize((width, int(width * im.height / im.width)))
    a = np.asarray(im).astype(float) / 255
    base = np.ones(a.shape[:2] + (3,)) * 0.95
    o = base
    for ch, col in ((0, s.col_green), (1, s.col_water), (2, s.col_road), (3, s.col_road)):
        c = np.array(col) ** (1 / 2.2)
        o = o * (1 - a[..., ch:ch + 1]) + c * a[..., ch:ch + 1]
    Image.fromarray((o * 255).astype("uint8")).save(path)
    return path
