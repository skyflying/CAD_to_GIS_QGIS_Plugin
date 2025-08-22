from __future__ import annotations
from typing import Dict, Tuple, Optional, List, Callable
from shapely.geometry import Point
import geopandas as gpd
import ezdxf

ProgressCB = Optional[Callable[[str], None]]

def _safe_s(val):
    try:
        if val is None: return None
        s = str(val)
        # normalize newlines for CSV/GPKG friendliness
        return s.replace("\r\n","\n").replace("\r","\n")
    except Exception:
        return None

def collect_annotations_and_blocks(
    dxf_path: str,
    *,
    src_epsg: int,
    tgt_epsg: Optional[int] = None,
    include_text: bool = True,
    include_blocks: bool = True,
    keep_block_transform: bool = True,
    on_progress: ProgressCB = None,
) -> Dict[Tuple[str, str], gpd.GeoDataFrame]:
    """Return extra buckets: TEXT/MTEXT annotations and BLOCK attributes.

    Buckets keys are (layer_name, geom_type). Geom type is always 'POINT' here.

    Layer naming:

      - TEXT/MTEXT: '<cad_layer>_TEXT'

      - BLOCKS: 'BLOCKS' (with 'block_name' column)

    """
    out: Dict[Tuple[str, str], gpd.GeoDataFrame] = {}

    def say(m):
        if on_progress:
            try: on_progress(m)
            except: pass

    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as ex:
        say(f"[augment] failed to read for annotations: {ex}")
        return out

    msp = doc.modelspace()

    # ---- TEXT / MTEXT as annotation points ----
    if include_text:
        rows_text: List[dict] = []
        for e in msp.query("TEXT MTEXT"):
            try:
                layer = (getattr(e.dxf, "layer", "0") or "0")
                if e.dxftype() == "TEXT":
                    ip = e.dxf.insert
                    txt = _safe_s(e.dxf.text)
                    rot = getattr(e.dxf, "rotation", 0.0) or 0.0
                    h = getattr(e.dxf, "height", None)
                    style = getattr(e.dxf, "style", None)
                else:  # MTEXT
                    ip = e.dxf.insert
                    # ezdxf MTEXT: .text or .plain_text()
                    try:
                        txt = _safe_s(e.plain_text())
                    except Exception:
                        txt = _safe_s(getattr(e, "text", None) or getattr(e.dxf, "text", None))
                    rot = getattr(e.dxf, "rotation", 0.0) or 0.0
                    h = getattr(e.dxf, "char_height", None)
                    style = getattr(e.dxf, "style", None)
                rows_text.append({
                    "layer": f"{layer}_TEXT",
                    "geom": "POINT",
                    "geometry": Point(ip.x, ip.y),
                    "text": txt,
                    "rotation": float(rot) if rot is not None else None,
                    "height": float(h) if h is not None else None,
                    "style": _safe_s(style),
                    "src_layer": layer,
                })
            except Exception:
                continue
        if rows_text:
            gdf = gpd.GeoDataFrame(rows_text, geometry="geometry", crs=f"EPSG:{src_epsg or 4326}")
            if tgt_epsg and int(tgt_epsg) != int(src_epsg or 4326):
                try: gdf = gdf.to_crs(epsg=int(tgt_epsg))
                except Exception: pass
            # group into <layer>_TEXT buckets
            for (layer, geom), sub in gdf.groupby(["layer","geom"]):
                out[(str(layer), str(geom))] = sub.reset_index(drop=True)

    # ---- BLOCK attributes as a point layer ----
    if include_blocks:
        rows_blk: List[dict] = []
        for ins in msp.query("INSERT"):
            try:
                layer = (getattr(ins.dxf, "layer", "0") or "0")
                ip = ins.dxf.insert
                row = {
                    "layer": "BLOCKS",
                    "geom": "POINT",
                    "geometry": Point(ip.x, ip.y),
                    "src_layer": layer,
                    "block_name": _safe_s(getattr(ins.dxf, "name", None) or getattr(ins, "name", None)),
                }
                # expand attributes
                try:
                    for a in ins.attribs():  # type: ignore[attr-defined]
                        tag = _safe_s(getattr(a.dxf, "tag", None))
                        val = _safe_s(getattr(a.dxf, "text", None))
                        if tag:
                            row[f"att_{tag}"] = val
                except Exception:
                    pass
                if keep_block_transform:
                    try:
                        row.update({
                            "ins_x": float(ip.x), "ins_y": float(ip.y), "ins_z": float(getattr(ip, "z", 0.0) or 0.0),
                            "rotation": float(getattr(ins.dxf, "rotation", 0.0) or 0.0),
                            "sx": float(getattr(ins.dxf, "xscale", 1.0) or 1.0),
                            "sy": float(getattr(ins.dxf, "yscale", 1.0) or 1.0),
                            "sz": float(getattr(ins.dxf, "zscale", 1.0) or 1.0),
                        })
                    except Exception:
                        pass
                rows_blk.append(row)
            except Exception:
                continue
        if rows_blk:
            gdfb = gpd.GeoDataFrame(rows_blk, geometry="geometry", crs=f"EPSG:{src_epsg or 4326}")
            if tgt_epsg and int(tgt_epsg) != int(src_epsg or 4326):
                try: gdfb = gdfb.to_crs(epsg=int(tgt_epsg))
                except Exception: pass
            out[("BLOCKS","POINT")] = gdfb.reset_index(drop=True)

    if not out:
        say("[augment] no TEXT/MTEXT or BLOCK attributes found")
    else:
        say(f"[augment] added {len(out)} extra bucket(s)")

    return out
