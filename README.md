# DWG to PDF map sheets in Python (1:1000 and 1:500)

Python replacement for the manual AutoCAD Civil 3D "DWG to PDF" process (VBA form + .scr script).

## Install
    pip install -r requirements.txt
DWG files need a converter on your PATH (Python cannot read DWG directly):
- ODA File Converter (free, Windows/Linux/Mac), or
- LibreDWG `dwg2dxf` (open source).
DXF files need no converter.

## Run
    python dwg_to_pdf.py C:\Temp\merge_process C:\Temp\Nigeria\print\pdffile --scale both --datum WGS84
Options: `--scale 500|1000|both`, `--datum WGS84|Minna`, `--anchor auto|SW|NW|SE|NE`,
`--workers N` (parallel tiles), `--only 7130-5220`, `--overwrite`.

## Output
- `7130-5220.pdf` (1:1000) and `7130-5220A.pdf` ... `D.pdf` (1:500 quarters A=SW, B=NW, C=NE, D=SE)
- `plot_log.csv` (replaces plot.log)

## Try it without real data
    python make_demo_tile.py sample_input 7130-5220     # synthetic tile, NOT survey data
    python dwg_to_pdf.py sample_input output --overwrite

## Known limits
- Tree/shrub/hedge SHX symbol linetypes draw as plain lines (SHX shapes are not supported).
- Quarter B uses geometry inferred from templates A, C, D (its .dwt would not convert).
- Grid label format (full metres, e.g. 522000) and the sheet-name corner (SW) are assumptions;
  `--anchor auto` checks the corner against the data.
