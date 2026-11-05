"""녹지/물 텍스처 (R=녹지, G=물) — 지형 전체 범위를 덮는 PNG."""
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as nd
from shapely import wkb
from .util import to_tm, Reporter

Image.MAX_IMAGE_PIXELS = None


def build_landcover(s, osm, terrain, rep: Reporter, out_png):
    xmin, ymin, xmax, ymax = terrain.extent
    res = s.lc_res
    while ((xmax - xmin) / res) * ((ymax - ymin) / res) > 120e6:
        res *= 1.25
    w, h = int((xmax - xmin) / res), int((ymax - ymin) / res)
    cx, cy = terrain.origin
    gx0, gy0 = xmin + cx, ymax + cy
    rep.log(f"   텍스처 {w}×{h}px ({res:.1f}m)")

    def draw(geoms, prog_a, prog_b):
        im = Image.new("L", (w, h), 0)
        dr = ImageDraw.Draw(im)
        n = len(geoms)
        for i, gb in enumerate(geoms):
            g = wkb.loads(gb)
            for pg in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
                for ring, val in [(pg.exterior, 255)] + [(r, 0) for r in pg.interiors]:
                    lon, lat = np.asarray(ring.coords).T[:2]
                    X, Y = to_tm(lon, lat)
                    px = (X - gx0) / res
                    py = (gy0 - Y) / res
                    if len(px) >= 3:
                        dr.polygon(list(zip(px.tolist(), py.tolist())), fill=val)
            if i % 500 == 0:
                rep.progress(prog_a + (prog_b - prog_a) * i / max(n, 1))
        return np.array(im) > 0

    green = draw(osm["green"], 0.0, 0.6)
    water = draw(osm["water"], 0.6, 0.85)
    # 지형에서 찾은 물 (한강·바다 등) 합치기
    tw = terrain.water.astype(bool)
    ys = np.clip(((np.arange(h) + 0.5) * res / terrain.res).astype(int), 0, terrain.H - 1)
    xs = np.clip(((np.arange(w) + 0.5) * res / terrain.res).astype(int), 0, terrain.W - 1)
    tw_up = tw[ys][:, xs]
    tw_up = nd.binary_erosion(tw_up, iterations=1)
    water |= tw_up
    green &= ~water

    def soft(m):
        return (nd.gaussian_filter(m.astype(np.float32), 0.7) * 255).clip(0, 255).astype(np.uint8)

    img = np.dstack([soft(green), soft(water), np.zeros((h, w), np.uint8)])
    Image.fromarray(img).save(out_png, optimize=True)
    rep.progress(1.0)
    rep.log(f"   녹지 {green.mean()*100:.1f}%, 물 {water.mean()*100:.1f}%")
    return out_png


def preview_png(lc_png, path, s, width=1000):
    im = Image.open(lc_png)
    im = im.resize((width, int(width * im.height / im.width)))
    a = np.asarray(im).astype(float) / 255
    base = np.ones(a.shape[:2] + (3,)) * 0.95
    g = np.array(s.col_green) ** (1 / 2.2)
    wcol = np.array(s.col_water) ** (1 / 2.2)
    o = base * (1 - a[..., :1]) + g * a[..., :1]
    o = o * (1 - a[..., 1:2]) + wcol * a[..., 1:2]
    Image.fromarray((o * 255).astype("uint8")).save(path)
    return path
