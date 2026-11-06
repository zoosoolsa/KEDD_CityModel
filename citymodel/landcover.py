"""지면 텍스처 (타일별 RGB PNG): R=녹지, G=물. 도로는 roads.py 에서 지형에 얹은 면으로 만든다."""
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

        def down(im):
            return np.asarray(im.resize((w, h), Image.BOX))

        green = down(fill(self.green))
        water = down(fill(self.water)).astype(np.float32)
        # 지형에서 찾은 물 (한강·바다 등): 25m 물 격자를 영역 전체에서 부드럽게 한 뒤 픽셀 중심에서 보간
        #  → 타일 경계에 테두리가 생기지 않고, 해안선이 계단 모양이 아니라 매끈한 선이 됨
        ws = getattr(terrain, "_water_smooth", None)
        if ws is None:
            ws = nd.gaussian_filter(terrain.water.astype(np.float32), 0.9)
            terrain._water_smooth = ws
        cx, cy = terrain.origin
        gx = x0 - cx + (np.arange(w) + 0.5) * res
        gy = y1 - cy - (np.arange(h) + 0.5) * res
        fx = (gx - terrain.x0) / terrain.res
        fy = (terrain.y0 - gy) / terrain.res
        FY, FX = np.meshgrid(fy, fx, indexing="ij")
        v = nd.map_coordinates(ws, [FY, FX], order=1, mode="nearest")
        tw = np.clip((v - 0.4) / 0.2, 0, 1) * 255
        water = np.maximum(water, tw)
        green = np.minimum(green.astype(np.float32), 255 - water)
        img = np.dstack([green, water, np.zeros_like(water)]).clip(0, 255).astype(np.uint8)
        return img


def preview_png(lc_png, path, s, width=1000):
    im = Image.open(lc_png).convert("RGB")
    im = im.resize((width, int(width * im.height / im.width)))
    a = np.asarray(im).astype(float) / 255
    base = np.ones(a.shape[:2] + (3,)) * 0.95
    o = base
    for ch, col in ((0, s.col_green), (1, s.col_water)):
        c = np.array(col) ** (1 / 2.2)
        o = o * (1 - a[..., ch:ch + 1]) + c * a[..., ch:ch + 1]
    Image.fromarray((o * 255).astype("uint8")).save(path)
    return path
