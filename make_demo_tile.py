#!/usr/bin/env python3
"""
make_demo_tile.py - build a SYNTHETIC map tile (DXF) so dwg_to_pdf.py can be tested
without the real survey drawings. It uses the layer names and colours of the Lagos
A0 template, so the PDF looks like a real sheet. The data is random, not real survey data.

    python make_demo_tile.py sample_input 7130-5220
"""
import math
import random
import sys
from pathlib import Path

import ezdxf

HERE = Path(__file__).resolve().parent


def make_tile(out_dir: Path, name: str = "7130-5220", anchor: str = "SW", seed: int = 7) -> Path:
    n100, e100 = int(name[:4]), int(name[5:])
    e0, n0 = e100 * 100.0, n100 * 100.0          # SW corner of the 1000 x 500 m sheet
    if "E" in anchor:
        e0 -= 1000
    if "N" in anchor:
        n0 -= 500
    rnd = random.Random(seed)

    tpl = ezdxf.readfile(HERE / "templates" / "A0SIZE1.dxf")
    doc = ezdxf.new("R2010", setup=True)
    for lay in tpl.layers:                         # same layers + colours as the template
        if lay.dxf.name not in doc.layers:
            doc.layers.add(lay.dxf.name, color=abs(lay.dxf.color))
    msp = doc.modelspace()

    # tree symbol block
    blk = doc.blocks.new("TREEPT")
    blk.add_circle((0, 0), 2.2)
    blk.add_line((-1.2, 0), (1.2, 0))
    blk.add_line((0, -1.2), (0, 1.2))

    def X(u):  # u in 0..1 across the sheet
        return e0 + u * 1000

    def Y(v):
        return n0 + v * 500

    # coastline / water body in the west, marsh around it
    shore = [(X(0.0), Y(1.0)), (X(0.18), Y(0.95)), (X(0.30), Y(0.78)), (X(0.42), Y(0.62)),
             (X(0.50), Y(0.40)), (X(0.62), Y(0.22)), (X(0.70), Y(0.0))]
    water = shore + [(X(0.0), Y(0.0))]
    msp.add_lwpolyline(shore, dxfattribs={"layer": "Coastline_Shoreline"})
    h = msp.add_hatch(color=5, dxfattribs={"layer": "Lake"})
    h.paths.add_polyline_path(water, is_closed=True)
    marsh = [(X(0.0), Y(0.0)), (X(0.62), Y(0.0)), (X(0.55), Y(0.30)), (X(0.35), Y(0.55)), (X(0.0), Y(0.7))]
    h = msp.add_hatch(color=3, dxfattribs={"layer": "Marsh_Swamp_Hatch"})
    h.set_pattern_fill("ANSI37", scale=2.5, angle=0)
    h.paths.add_polyline_path(marsh, is_closed=True)

    # contours with annotations
    for k, v0 in enumerate([0.15, 0.3, 0.45, 0.6, 0.75, 0.9]):
        pts = [(X(u / 40), Y(v0) + 18 * math.sin(u / 5 + k)) for u in range(41)]
        layer = "MajorContour" if k % 2 == 0 else "MinorContour"
        msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
        msp.add_text(f"{2.5 + k * 1.5:.1f}", height=3,
                     dxfattribs={"layer": layer + "_Annotation"}).set_placement(pts[20])

    # road network (two curb lines each) and paved drives
    for (a, b) in [((0.45, 0.0), (0.55, 1.0)), ((0.2, 0.55), (1.0, 0.65)), ((0.7, 0.0), (0.78, 1.0))]:
        for off in (-5, 5):
            msp.add_line((X(a[0]) + off, Y(a[1])), (X(b[0]) + off, Y(b[1])), dxfattribs={"layer": "RoadCurb"})

    # buildings (outline + solid fill) mostly in the east
    for _ in range(140):
        cx, cy = X(rnd.uniform(0.45, 0.98)), Y(rnd.uniform(0.05, 0.95))
        w, d, ang = rnd.uniform(8, 28), rnd.uniform(8, 22), math.radians(rnd.uniform(-20, 20))
        pts = [(cx + dx * math.cos(ang) - dy * math.sin(ang), cy + dx * math.sin(ang) + dy * math.cos(ang))
               for dx, dy in [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "Building"})
        hh = msp.add_hatch(color=7, dxfattribs={"layer": "Building_Hatch"})
        hh.set_solid_fill()
        hh.paths.add_polyline_path(pts, is_closed=True)

    # trees, manholes, spot heights
    for _ in range(120):
        msp.add_blockref("TREEPT", (X(rnd.uniform(0.3, 1)), Y(rnd.uniform(0, 1))), dxfattribs={"layer": "Tree"})
    for _ in range(25):
        p = (X(rnd.random()), Y(rnd.random()))
        msp.add_circle(p, 1.2, dxfattribs={"layer": "Manhole"})
        msp.add_text(f"{rnd.uniform(1.5, 12):.2f}", height=2.5, dxfattribs={"layer": "Spot_Annotation"
                     if "Spot_Annotation" in doc.layers else "0"}).set_placement((p[0] + 2, p[1] + 2))

    # things OUTSIDE this sheet - must be clipped away by the map frame
    msp.add_lwpolyline([(e0 - 400, n0 - 200), (e0 + 1400, n0 + 700)], dxfattribs={"layer": "RoadCurb"})
    msp.add_text("OUTSIDE-SHEET-TEST", height=20, dxfattribs={"layer": "Building"}).set_placement((e0 - 600, n0 + 250))

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.dxf"
    doc.saveas(path)
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_input")
    nm = sys.argv[2] if len(sys.argv) > 2 else "7130-5220"
    print(make_tile(out, nm))
