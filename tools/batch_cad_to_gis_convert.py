from __future__ import annotations
import argparse, os, sys, time
from typing import List, Optional, Tuple

# Local imports: expect sibling 'services' package in parent
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from services.conversion_service import precise_convert, write_outputs
try:
    from services.dwg_support import dwg_to_temp_dxf_auto
except Exception:
    dwg_to_temp_dxf_auto = None  # optional

def find_cad_files(root: str, exts: Tuple[str, ...] = (".dxf", ".dwg")) -> List[str]:
    out: List[str] = []
    for base, _dirs, files in os.walk(root):
        for fn in files:
            if os.path.splitext(fn)[1].lower() in exts:
                out.append(os.path.join(base, fn))
    return sorted(out)

def log(msg: str):
    print(msg, flush=True)

def convert_one(cad_path: str, source_epsg: int, target_epsg: Optional[int],
                target_layers: Optional[List[str]], block_mode: str,
                line_merge_tol: float, driver: str, out_path: str, overwrite: bool,
                dwg_prefer: str, dxf_version: str) -> List[dict]:
    input_for_convert = cad_path
    if cad_path.lower().endswith(".dwg"):
        if dwg_to_temp_dxf_auto is None:
            raise RuntimeError("DWG given but no converter available.")
        log(f"[DWG] Converting to DXF: {cad_path}")
        input_for_convert = dwg_to_temp_dxf_auto(cad_path, prefer=dwg_prefer, dxf_version=dxf_version)
    t0 = time.time()
    buckets = precise_convert([input_for_convert], source_epsg=source_epsg, target_epsg=target_epsg,
                              include_3d=False, bbox_wgs84=None, target_layers=target_layers,
                              block_mode=block_mode, line_merge_tol=line_merge_tol,
                              fallback_explode_lines=True, on_progress=lambda m: log(f"    {m}"))
    dt = time.time() - t0
    log(f"[OK] Converted in {dt:.1f}s; writing …")
    written = write_outputs(buckets, out_path=out_path,
                            driver=('GPKG' if driver.upper()=='GPKG' else 'ESRI Shapefile'),
                            overwrite=overwrite, on_progress=lambda m: log(f"    {m}")) or []
    return written

def main():
    ap = argparse.ArgumentParser(description="Batch CAD (DXF/DWG) to GIS converter")
    ap.add_argument("--input-root", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--driver", default="GPKG", choices=["GPKG", "ESRI_SHP"])
    ap.add_argument("--src-epsg", type=int, default=3826)
    ap.add_argument("--tgt-epsg", type=int, default=None)
    ap.add_argument("--layers", default="")
    ap.add_argument("--mode", default="keep-merge", choices=["keep-merge","explode"])
    ap.add_argument("--merge-tol", type=float, default=0.2)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dwg-prefer", default="auto", choices=["auto","oda","libredwg"])
    ap.add_argument("--dxf-version", default="ACAD2013")
    args = ap.parse_args()

    input_root = os.path.abspath(args.input_root)
    driver = args.driver.upper()
    output = os.path.abspath(args.output)

    if not os.path.isdir(input_root):
        ap.error(f"Input root not found: {input_root}")

    if driver == "GPKG":
        out_gpkg = output
        os.makedirs(os.path.dirname(out_gpkg) or ".", exist_ok=True)
    else:
        os.makedirs(output, exist_ok=True)

    target_layers = [s.strip() for s in args.layers.split(',') if s.strip()] if args.layers else None

    cad_files = find_cad_files(input_root)
    if not cad_files:
        print("[INFO] No DXF/DWG files found.")
        return

    print(f"[INFO] Found {len(cad_files)} CAD files.")
    total_written = 0

    for idx, cad in enumerate(cad_files, 1):
        print(f"[{idx}/{len(cad_files)}] {cad}")
        try:
            if driver == "GPKG":
                written = convert_one(cad, args.src_epsg, args.tgt_epsg, target_layers,
                                      args.mode, args.merge_tol, driver, out_gpkg,
                                      args.overwrite, args.dwg_prefer, args.dxf_version)
            else:
                rel = os.path.relpath(os.path.dirname(cad), input_root)
                sub = os.path.join(output, rel, os.path.splitext(os.path.basename(cad))[0])
                os.makedirs(sub, exist_ok=True)
                written = convert_one(cad, args.src_epsg, args.tgt_epsg, target_layers,
                                      args.mode, args.merge_tol, driver, sub,
                                      args.overwrite, args.dwg_prefer, args.dxf_version)
            count = sum((w.get('count') or 0) for w in written)
            total_written += count
            print(f"[DONE] Layers: {len(written)} | Features: {count}")
        except Exception as ex:
            print(f"[ERROR] {ex}")

    print(f"[SUMMARY] Total features written: {total_written}")

if __name__ == "__main__":
    main()
