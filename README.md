# DWG to Scaled PDF Map Sheets

A Python replacement for a manual AutoCAD Civil 3D map-sheet plotting workflow. It converts DWG inputs through an installed CAD converter, processes DXF directly, places the map geometry on 1:1000 and 1:500 sheets, and writes PDFs plus a CSV plot log.

Install dependencies with `pip install -r requirements.txt`. DWG conversion requires ODA File Converter or LibreDWG on `PATH`; DXF inputs do not need that external conversion step.

```sh
python dwg_to_pdf.py C:\Temp\merge_process C:\Temp\print\pdf --scale both --datum WGS84
```

Use `--scale 500|1000|both`, `--datum WGS84|Minna`, `--anchor auto|SW|NW|SE|NE`, `--workers N`, `--only SHEET`, and `--overwrite` to control the output. `make_demo_tile.py` creates a synthetic sample tile for a local workflow demonstration.
