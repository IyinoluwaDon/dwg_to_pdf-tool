#!/usr/bin/env python3
"""
dwg_to_pdf.py - Batch DWG -> A0 PDF map sheets (1:1000 and 1:500) without AutoCAD macros.

Python replacement for the manual "Process for DWG to PDF" (AutoCAD Civil 3D 2012 +
Print.VLX + Print_plot.dvb + .scr files). What the old workflow did by hand and what
this script does instead:

    AutoCAD workflow                              This script
    --------------------------------------------  -------------------------------------------
    setprint.bat (create folders)                 Path.mkdir()
    Options: save as DWG 2000, plotter = PDF.pc3  convert_to_dxf() + matplotlib PDF backend
    VBA form: pick scale + datum (WGS84/Minna)    --scale 500|1000|both  --datum WGS84|Minna
    "SCR file creation" (.exe -> *.scr list)      build_jobs() (a plain list of tiles)
    "RUN Script" (open, hone, Height, print, close)process_tile() (one tile at a time)
    A0SIZE1/A0SIZE501..504.dwt layout templates   templates/A0SIZE1.dxf, A0SIZE501.dxf
    INDEX.csv / INDEX_Minna.csv (map index)       load_index() + neighbours()
    plot.log                                      plot_log.csv
    "Close and reopen AutoCAD for each batch"     one worker process per tile (memory freed)

Sheet system (derived from the templates and screenshots):
    * Tile name "7130-5220" = Northing/100 - Easting/100 of the sheet corner (default SW).
    * 1:1000 sheet = 1000 m (E-W) x 500 m (N-S) on a 1000 x 500 mm map frame.
    * 1:500 sheets = the four quarters of that tile, A (SW) B (NW) C (NE) D (SE),
      each 500 m x 250 m on the same 1000 x 500 mm frame.
    * Map index = the 3 x 3 neighbouring sheets: N +/- 5, E +/- 10 (in 100 m units).

Usage:
    python dwg_to_pdf.py INPUT_DIR OUTPUT_DIR --scale both --datum WGS84 --workers 2
"""
from __future__ import annotations

import argparse
import csv
import getpass
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import ezdxf
import matplotlib

matplotlib.use("Agg")  # no display needed
import matplotlib.pyplot as plt
from ezdxf import bbox, recover
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle
from PIL import Image

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
RESOURCES = HERE / "resources"

PAGE_W_MM, PAGE_H_MM = 1189.0, 841.0  # ISO A0 landscape (same as DWG To PDF.pc3 setting)
MM_PER_INCH = 25.4
PT_PER_MM = 72.0 / MM_PER_INCH

SHEET_RE = re.compile(r"^(\d{4})-(\d{4})$")
NORTH_STEP, EAST_STEP = 5, 10          # neighbouring sheet offsets, in 100 m units
SHEET_W_M, SHEET_H_M = 1000.0, 500.0   # one 1:1000 sheet on the ground

# 1:500 quarter sheets: (column, row) of the quarter inside the 1:1000 sheet. Row 0 = south.
QUADRANTS = {"A": (0, 0), "B": (0, 1), "C": (1, 1), "D": (1, 0)}  # SW, NW, NE, SE


@dataclass(frozen=True)
class ScaleCfg:
    scale: int
    template: str
    window_w_m: float      # ground width shown in the map frame
    window_h_m: float
    label_step_m: float    # ground distance between grid lines / labels


SCALES = {
    1000: ScaleCfg(1000, "A0SIZE1.dxf", 1000.0, 500.0, 100.0),
    500: ScaleCfg(500, "A0SIZE501.dxf", 500.0, 250.0, 50.0),
}


# ----------------------------------------------------------------------------- sheet maths
@dataclass(frozen=True)
class Sheet:
    """A 1:1000 tile such as 7130-5220 and the ground area it covers."""
    name: str
    n100: int
    e100: int
    anchor: str = "SW"  # which corner of the sheet the name refers to

    @classmethod
    def parse(cls, stem: str, anchor: str = "SW") -> "Sheet":
        m = SHEET_RE.match(stem)
        if not m:
            raise ValueError(f"'{stem}' is not a sheet name like 7130-5220")
        return cls(stem, int(m.group(1)), int(m.group(2)), anchor)

    def with_anchor(self, anchor: str) -> "Sheet":
        return Sheet(self.name, self.n100, self.e100, anchor)

    def origin_m(self) -> tuple[float, float]:
        """(easting_min, northing_min) of the 1000 x 500 m sheet."""
        e, n = self.e100 * 100.0, self.n100 * 100.0
        if "E" in self.anchor:
            e -= SHEET_W_M
        if "N" in self.anchor:
            n -= SHEET_H_M
        return e, n

    def window(self, scale: int, quadrant: str | None) -> tuple[float, float, float, float]:
        """Ground window (xmin, ymin, xmax, ymax) printed on one sheet."""
        e0, n0 = self.origin_m()
        if scale == 1000:
            return e0, n0, e0 + SHEET_W_M, n0 + SHEET_H_M
        col, row = QUADRANTS[quadrant]
        w, h = SHEET_W_M / 2, SHEET_H_M / 2
        return e0 + col * w, n0 + row * h, e0 + (col + 1) * w, n0 + (row + 1) * h

    def neighbours(self, index: set[str]) -> list[list[str]]:
        """3x3 map index (north row first). A name outside the survey index becomes ''."""
        grid = []
        for dr in (+1, 0, -1):
            row = []
            for dc in (-1, 0, +1):
                nm = f"{self.n100 + dr * NORTH_STEP:04d}-{self.e100 + dc * EAST_STEP:04d}"
                row.append(nm if (nm in index or (dr == 0 and dc == 0)) else "")
            grid.append(row)
        return grid


def load_index(datum: str) -> set[str]:
    """INDEX.csv (WGS84) or INDEX_Minna.csv: a grid of sheet names, 0000-0000 = no sheet."""
    fn = RESOURCES / ("INDEX_Minna.csv" if datum.lower() == "minna" else "INDEX.csv")
    names: set[str] = set()
    with open(fn, newline="") as fh:
        for row in csv.reader(fh):
            names.update(c.strip() for c in row if SHEET_RE.match(c.strip()) and c.strip() != "0000-0000")
    return names


# ----------------------------------------------------------------------------- DWG reading
def convert_to_dxf(src: Path, work: Path) -> Path:
    """DWG -> DXF. Python cannot read DWG natively, so use ODA File Converter or LibreDWG."""
    if src.suffix.lower() == ".dxf":
        return src
    out = work / (src.stem + ".dxf")
    oda = shutil.which("ODAFileConverter") or shutil.which("ODAFileConverter.exe")
    dwg2dxf = shutil.which("dwg2dxf")
    if oda:  # ODAFileConverter <in_dir> <out_dir> <out_ver> <out_type> <recurse> <audit> [filter]
        subprocess.run([oda, str(src.parent), str(work), "ACAD2018", "DXF", "0", "1", src.name],
                       check=True, capture_output=True, timeout=600)
    elif dwg2dxf:  # LibreDWG
        subprocess.run([dwg2dxf, "-y", "-o", str(out), str(src)],
                       capture_output=True, timeout=600)  # warnings are normal, check the file
    else:
        raise RuntimeError(
            "No DWG converter found. Install ODA File Converter (free) or LibreDWG's dwg2dxf, "
            "or save the drawings as DXF and run again.")
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"DWG conversion produced no output for {src.name}")
    return out


def read_dxf(path: Path):
    try:
        return ezdxf.readfile(path)
    except ezdxf.DXFError:
        doc, _auditor = recover.readfile(path)  # tolerant reader for slightly damaged files
        return doc


# ----------------------------------------------------------------------------- tile data
class TileData:
    """A loaded tile: the DXF document plus a bounding-box index of its model-space entities."""

    def __init__(self, doc):
        self.doc = doc
        self.msp = doc.modelspace()
        cache = bbox.Cache()
        self.boxes: list[tuple[object, tuple[float, float, float, float] | None]] = []
        for e in self.msp:
            try:
                b = bbox.extents([e], fast=True, cache=cache)
            except Exception:
                b = None
            if b is not None and b.has_data:
                self.boxes.append((e, (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)))
            else:
                self.boxes.append((e, None))

    def centre(self) -> tuple[float, float] | None:
        cs = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for _, b in self.boxes if b]
        if not cs:
            return None
        return statistics.median(c[0] for c in cs), statistics.median(c[1] for c in cs)

    def outside(self, win, pad=0.05) -> set[int]:
        """ids of entities that lie completely outside the window (skipped when drawing)."""
        x0, y0, x1, y1 = win
        px, py = (x1 - x0) * pad, (y1 - y0) * pad
        return {id(e) for e, b in self.boxes
                if b and (b[2] < x0 - px or b[0] > x1 + px or b[3] < y0 - py or b[1] > y1 + py)}


def detect_anchor(sheet: Sheet, tile: TileData, default: str = "SW") -> str:
    """Which corner of the sheet does the file name refer to? Test it against the real data."""
    c = tile.centre()
    if c is None:
        return default
    hits = []
    for a in ("SW", "NW", "SE", "NE"):
        x0, y0, x1, y1 = sheet.with_anchor(a).window(1000, None)
        if x0 <= c[0] <= x1 and y0 <= c[1] <= y1:
            hits.append(a)
    if len(hits) == 1:
        return hits[0]
    return default if (not hits or default in hits) else hits[0]


# ----------------------------------------------------------------------------- template
@dataclass
class Furniture:
    layout: object
    frame: tuple[float, float, float, float]   # map frame in layout units
    mm_per_unit: float
    table: tuple[float, float, float, float]   # map index box in layout units
    images: list[dict]
    centre: tuple[float, float]                # centre of the whole drawing, layout units


def prepare_template(cfg: ScaleCfg, sheet_label: str, datum: str) -> tuple[object, Furniture]:
    """Load the A0 layout template, read its geometry, strip the parts we regenerate."""
    doc = ezdxf.readfile(TEMPLATES / cfg.template)
    layout = next(l for l in doc.layouts if l.name != "Model")

    vp = min((e for e in layout if e.dxftype() == "VIEWPORT"),
             key=lambda v: v.dxf.width * v.dxf.height)  # the small one is the map window
    cx, cy, w, h = vp.dxf.center.x, vp.dxf.center.y, vp.dxf.width, vp.dxf.height
    frame = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    mm_per_unit = 1000.0 / w  # the map frame is always 1000 mm wide on paper

    table = bbox.extents([e for e in layout if e.dxf.layer == "Table"])
    images = []
    for e in layout:
        t = e.dxftype()
        if t == "IMAGE" and e.image_def is not None:
            px_w, px_h = e.dxf.image_size.x, e.dxf.image_size.y
            x, y = e.dxf.insert.x, e.dxf.insert.y
            images.append(dict(file=Path(e.image_def.dxf.filename.replace("\\", "/")).name,
                               extent=(x, x + e.dxf.u_pixel.x * px_w, y, y + e.dxf.v_pixel.y * px_h)))
        elif t in ("TEXT", "MTEXT"):
            txt = e.dxf.text if t == "TEXT" else e.text
            if txt.strip() == "SHEET:-":
                e.dxf.text = f"SHEET:- {sheet_label}"
            elif t == "MTEXT" and "\\pi" in txt:        # hanging-indent codes make ezdxf wrap the text
                e.text = re.sub(r"\\pi[^;]*;", "", txt)
            elif e.dxf.layer == "Minna" and txt.strip().upper().startswith(("WGS", "MINNA")):
                new = "MINNA" if datum.lower() == "minna" else "WGS 84"
                if t == "TEXT":
                    e.dxf.text = new
                else:
                    e.text = new

    # remove what the script redraws itself (labels, map index, images, viewports)
    for e in list(layout):
        if (e.dxf.layer.startswith("Grid_Text") or e.dxf.layer == "Table"
                or e.dxftype() in ("IMAGE", "VIEWPORT")):
            layout.delete_entity(e)

    ext = bbox.extents(list(layout))
    centre = ((ext.extmin.x + ext.extmax.x) / 2, (ext.extmin.y + ext.extmax.y) / 2)
    return doc, Furniture(layout, frame, mm_per_unit,
                          (table.extmin.x, table.extmin.y, table.extmax.x, table.extmax.y),
                          images, centre)


# ----------------------------------------------------------------------------- drawing
def _ezdxf_draw(ax, doc, layout, filter_func=None):
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, min_lineweight=0.12)
    backend = MatplotlibBackend(ax)
    Frontend(RenderContext(doc), backend, config=cfg).draw_layout(
        layout, finalize=False, filter_func=filter_func)


def _draw_grid_labels(ax, fur: Furniture, win, cfg: ScaleCfg):
    x0, y0, x1, y1 = fur.frame
    mpu = fur.mm_per_unit
    fs = 2.0 * PT_PER_MM                       # 2 mm high text, like the template (height 2/1)
    step_u = (x1 - x0) * cfg.label_step_m / cfg.window_w_m
    top_bot, side = 5.0 / mpu, 11.8 / mpu      # label offsets from the frame (mm -> units)
    nx = round(cfg.window_w_m / cfg.label_step_m)
    ny = round(cfg.window_h_m / cfg.label_step_m)
    kw = dict(fontsize=fs, ha="center", va="center", color="black", zorder=5)
    for i in range(nx + 1):
        label = f"{win[0] + i * cfg.label_step_m:.0f}"
        ax.text(x0 + i * step_u, y1 + top_bot, label, **kw)
        ax.text(x0 + i * step_u, y0 - top_bot, label, **kw)
    for j in range(ny + 1):
        label = f"{win[1] + j * cfg.label_step_m:.0f}"
        ax.text(x0 - side, y0 + j * step_u, label, **kw)
        ax.text(x1 + side, y0 + j * step_u, label, **kw)


def _draw_map_index(ax, fur: Furniture, sheet: Sheet, index: set[str], quadrant: str | None):
    x0, y0, x1, y1 = fur.table
    cw, ch = (x1 - x0) / 3, (y1 - y0) / 3
    grid = sheet.neighbours(index)
    lw_thin, lw_bold = 0.6, 1.8
    fs = 0.72 * cw * fur.mm_per_unit / (9 * 0.6) * PT_PER_MM      # 9 chars fit in ~72 % of a cell
    for r in range(3):                         # r = 0 is the north row
        for c in range(3):
            cx0, cy0 = x0 + c * cw, y1 - (r + 1) * ch
            ax.add_patch(Rectangle((cx0, cy0), cw, ch, fill=False, lw=lw_thin, ec="black", zorder=4))
            name = grid[r][c]
            if (r, c) == (1, 1):
                continue
            if name:
                ax.text(cx0 + cw / 2, cy0 + ch / 2, name, fontsize=fs, ha="center", va="center",
                        color="#444444", zorder=5)
    cx0, cy0 = x0 + cw, y0 + ch               # centre cell = this sheet
    if quadrant is None:
        ax.add_patch(Rectangle((cx0, cy0), cw, ch, fill=False, hatch="/////", lw=0, ec="#888888", zorder=4))
        m = 0.14
        ax.add_patch(Rectangle((cx0 + m * cw, cy0 + 0.25 * ch), cw * (1 - 2 * m), ch * 0.5,
                               fc="white", ec="black", lw=lw_bold, zorder=5))
        ax.text(cx0 + cw / 2, cy0 + ch / 2, sheet.name, fontsize=fs, ha="center", va="center", zorder=6)
    else:                                     # split the centre cell in four, highlight the quarter
        hw, hh = cw / 2, ch / 2
        for q, (qc, qr) in QUADRANTS.items():
            qx, qy = cx0 + qc * hw, cy0 + qr * hh
            on = q == quadrant
            ax.add_patch(Rectangle((qx, qy), hw, hh, fc="white", ec="black",
                                   lw=lw_bold if on else lw_thin, zorder=5 if on else 4))
            ax.text(qx + hw / 2, qy + hh / 2, q, fontsize=fs * (1.05 if on else 0.9),
                    fontweight="bold" if on else "normal", ha="center", va="center",
                    color="black" if on else "#777777", zorder=6)


def _draw_logos(ax, fur: Furniture):
    for im in fur.images:
        p = RESOURCES / im["file"]
        if p.exists():
            ax.imshow(Image.open(p).convert("RGB"), extent=im["extent"], aspect="auto", zorder=3, interpolation="bilinear")


def render_sheet(sheet: Sheet, tile: TileData, scale: int, quadrant: str | None,
                 datum: str, index: set[str], out_pdf: Path) -> None:
    cfg = SCALES[scale]
    label = f"{sheet.name} {quadrant}" if quadrant else sheet.name
    win = sheet.window(scale, quadrant)

    tpl_doc, fur = prepare_template(cfg, label, datum)
    mpu = fur.mm_per_unit
    wu, hu = PAGE_W_MM / mpu, PAGE_H_MM / mpu                       # page size in layout units
    px0, py0 = fur.centre[0] - wu / 2, fur.centre[1] - hu / 2       # centre the sheet on the A0 page

    fig = plt.figure(figsize=(PAGE_W_MM / MM_PER_INCH, PAGE_H_MM / MM_PER_INCH), facecolor="white")

    # 1) the map data, clipped to the map frame (this is what the AutoCAD viewport did)
    fx0, fy0, fx1, fy1 = fur.frame
    ax_map = fig.add_axes([(fx0 - px0) / wu, (fy0 - py0) / hu, (fx1 - fx0) / wu, (fy1 - fy0) / hu])
    skip = tile.outside(win)
    _ezdxf_draw(ax_map, tile.doc, tile.msp, filter_func=lambda e: id(e) not in skip)
    ax_map.set_aspect("auto")
    ax_map.set_xlim(win[0], win[2])
    ax_map.set_ylim(win[1], win[3])
    ax_map.set_axis_off()

    # 2) the sheet furniture on top: border, grid, legend, title block, north arrow, scale bar
    ax = fig.add_axes([0, 0, 1, 1], facecolor="none")
    _ezdxf_draw(ax, tpl_doc, fur.layout)
    _draw_logos(ax, fur)
    _draw_grid_labels(ax, fur, win, cfg)
    _draw_map_index(ax, fur, sheet, index, quadrant)
    ax.set_aspect("auto")
    ax.set_xlim(px0, px0 + wu)
    ax.set_ylim(py0, py0 + hu)
    ax.set_axis_off()

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out_pdf, metadata={"Title": f"Sheet {label} 1:{scale}",
                                     "Author": "Interspatial Technologies",
                                     "Subject": f"Lagos State, Nigeria - datum {datum}"}) as pdf:
        pdf.savefig(fig, dpi=300)
    plt.close(fig)


# ----------------------------------------------------------------------------- batch driver
def process_tile(job: dict) -> list[dict]:
    """Everything for ONE tile. Runs in its own process, so memory is freed afterwards."""
    src, out_dir = Path(job["src"]), Path(job["out_dir"])
    scales, datum = job["scales"], job["datum"]
    rows: list[dict] = []
    base = dict(dwg=str(src), user=getpass.getuser(), device="ezdxf + matplotlib (vector PDF)",
                paper="ISO A0 (841.00 x 1189.00 MM)")
    try:
        sheet = Sheet.parse(src.stem)
        index = load_index(datum)
        with tempfile.TemporaryDirectory(prefix="dwg2pdf_") as tmp:
            dxf = convert_to_dxf(src, Path(tmp))
            tile = TileData(read_dxf(dxf))
            anchor = detect_anchor(sheet, tile) if job["anchor"] == "auto" else job["anchor"]
            sheet = sheet.with_anchor(anchor)
            todo = []
            if 1000 in scales:
                todo.append((1000, None))
            if 500 in scales:
                todo += [(500, q) for q in QUADRANTS]
            for scale, q in todo:
                pdf = out_dir / f"{sheet.name}{q or ''}.pdf"
                t0 = time.time()
                row = dict(base, layout=f"{sheet.name}{q or ''}", scale=f"1:{scale}", pdf=str(pdf),
                           timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                if pdf.exists() and not job["overwrite"]:
                    row.update(status="SKIPPED (exists, use --overwrite)", seconds=0)
                else:
                    try:
                        render_sheet(sheet, tile, scale, q, datum, index, pdf)
                        row.update(status=f"OK (anchor {anchor})", seconds=round(time.time() - t0, 1))
                    except Exception as exc:  # keep going with the other sheets
                        row.update(status=f"FAILED: {exc}", seconds=round(time.time() - t0, 1))
                        traceback.print_exc()
                rows.append(row)
    except Exception as exc:
        rows.append(dict(base, layout="-", scale="-", pdf="-", timestamp=datetime.now().isoformat(" ", "seconds"),
                         seconds=0, status=f"FAILED: {exc}"))
    return rows


LOG_FIELDS = ["dwg", "layout", "timestamp", "user", "device", "paper", "scale", "pdf", "seconds", "status"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Batch DWG/DXF tiles -> A0 PDF map sheets (1:1000 / 1:500)")
    ap.add_argument("input_dir", type=Path, help="folder with tiles named like 7130-5220.dwg (or .dxf)")
    ap.add_argument("output_dir", type=Path, help="folder for the PDFs and plot_log.csv")
    ap.add_argument("--scale", choices=["500", "1000", "both"], default="both")
    ap.add_argument("--datum", choices=["WGS84", "Minna"], default="WGS84")
    ap.add_argument("--anchor", choices=["auto", "SW", "NW", "SE", "NE"], default="auto",
                    help="corner of the sheet that the file name refers to (auto = test against the data)")
    ap.add_argument("--workers", type=int, default=1, help="tiles processed in parallel")
    ap.add_argument("--only", nargs="*", help="process only these tile names, e.g. 7130-5220")
    ap.add_argument("--overwrite", action="store_true", help="replace PDFs that already exist")
    a = ap.parse_args(argv)

    files = sorted(p for p in a.input_dir.iterdir() if p.suffix.lower() in (".dwg", ".dxf"))
    if a.only:
        files = [p for p in files if p.stem in a.only]
    bad = [p.name for p in files if not SHEET_RE.match(p.stem)]
    files = [p for p in files if SHEET_RE.match(p.stem)]
    if bad:
        print("Skipping (name is not like 7130-5220):", ", ".join(bad))
    if not files:
        print("No tiles to process.")
        return 1

    scales = [500, 1000] if a.scale == "both" else [int(a.scale)]
    a.output_dir.mkdir(parents=True, exist_ok=True)
    jobs = [dict(src=str(p), out_dir=str(a.output_dir), scales=scales, datum=a.datum,
                 anchor=a.anchor, overwrite=a.overwrite) for p in files]

    log_path = a.output_dir / "plot_log.csv"
    new_log = not log_path.exists()
    ok = failed = 0
    with open(log_path, "a", newline="") as fh, \
            ProcessPoolExecutor(max_workers=max(1, a.workers), max_tasks_per_child=1) as pool:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new_log:
            w.writeheader()
        for fut in as_completed([pool.submit(process_tile, j) for j in jobs]):
            for row in fut.result():
                w.writerow({k: row.get(k, "") for k in LOG_FIELDS})
                fh.flush()
                flag = "ok " if row["status"].startswith(("OK", "SKIPPED")) else "ERR"
                ok += flag == "ok "
                failed += flag == "ERR"
                print(f"[{flag}] {row['layout']:<14} {row['scale']:<7} {row['status']}")
    print(f"\nDone: {ok} sheet(s) written/skipped, {failed} failed. Log: {log_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
