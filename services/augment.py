from __future__ import annotations
from typing import Dict, Tuple, Optional, List, Callable, Iterable
import logging

_log = logging.getLogger(__name__)

# Heavy imports are deferred: this module is imported when the plugin loads, and
# QGIS must be able to load the plugin even if shapely/geopandas are not installed.
gpd = None


def _ensure_gpd():
    global gpd
    if gpd is None:
        import geopandas as _gpd
        gpd = _gpd
    return gpd

ProgressCB = Optional[Callable[[str], None]]

def _say(cb: ProgressCB, msg: str):
    if cb:
        try: cb(msg)
        except Exception:
            _log.debug("_say: ignored exception", exc_info=True)

def _safe_s(val):
    try:
        if val is None: return None
        s = str(val)
        return s.replace("\r\n","\n").replace("\r","\n")
    except Exception:
        return None

def _in_selected(layer_name: str, selected: Optional[Iterable[str]]) -> bool:
    # empty / None selection means ALL layers (same rule as the main conversion)
    return (not selected) or (layer_name in selected)

def collect_annotations_and_blocks(
    dxf_path: str,
    *,
    src_epsg: int,
    tgt_epsg: Optional[int] = None,
    include_text: bool = True,
    include_blocks: bool = True,
    keep_block_transform: bool = True,
    target_layers: Optional[List[str]] = None,
    on_progress: ProgressCB = None,
):
    """Return extra buckets (TEXT/MTEXT & BLOCKS) filtered by selected CAD layers.

    - Only entities whose *source CAD layer* is present in target_layers will be extracted.

    - If target_layers is empty/None, ALL layers are used (the old version extracted
      nothing in that case, although the UI says "leave empty for all").

    """
    try:
        _ensure_gpd()
        from shapely.geometry import Point
    except Exception as ex:
        raise RuntimeError(f"geopandas/shapely are required for TEXT/MTEXT/BLOCK extraction: {ex}")

    import ezdxf
    out: Dict[Tuple[str,str], "gpd.GeoDataFrame"] = {}

    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as ex:
        _say(on_progress, f"[augment] failed to read for annotations: {ex}")
        return out

    msp = doc.modelspace()
    sel = set(target_layers or [])

    # TEXT / MTEXT
    if include_text:
        rows_text: List[dict] = []
        for e in msp.query("TEXT MTEXT"):
            try:
                src_layer = (getattr(e.dxf, "layer", "0") or "0")
                if not _in_selected(src_layer, sel):
                    continue  # filter by selected layers
                if e.dxftype() == "TEXT":
                    ip = e.dxf.insert
                    txt = _safe_s(e.dxf.text); rot = getattr(e.dxf, "rotation", 0.0) or 0.0
                    h = getattr(e.dxf, "height", None); style = getattr(e.dxf, "style", None)
                else:
                    ip = e.dxf.insert
                    try: txt = _safe_s(e.plain_text())
                    except Exception: txt = _safe_s(getattr(e, "text", None) or getattr(e.dxf, "text", None))
                    rot = getattr(e.dxf, "rotation", 0.0) or 0.0
                    h = getattr(e.dxf, "char_height", None); style = getattr(e.dxf, "style", None)
                rows_text.append({
                    "layer": f"{src_layer}_TEXT", "geom": "POINT", "geometry": Point(ip.x, ip.y),
                    "text": txt, "rotation": float(rot) if rot is not None else None,
                    "height": float(h) if h is not None else None, "style": _safe_s(style),
                    "src_layer": src_layer,
                })
            except Exception:
                _log.debug("collect_annotations_and_blocks: ignored exception", exc_info=True)
                continue
        if rows_text:
            gdf = gpd.GeoDataFrame(rows_text, geometry="geometry", crs=f"EPSG:{src_epsg or 4326}")
            if tgt_epsg and int(tgt_epsg) != int(src_epsg or 4326):
                try: gdf = gdf.to_crs(epsg=int(tgt_epsg))
                except Exception:
                    _log.debug("collect_annotations_and_blocks: ignored exception", exc_info=True)
            for (layer, geom), sub in gdf.groupby(["layer","geom"]):
                out[(str(layer), str(geom))] = sub.reset_index(drop=True)

    # BLOCK INSERT (attributes)
    if include_blocks:
        rows_blk: List[dict] = []
        for ins in msp.query("INSERT"):
            try:
                src_layer = (getattr(ins.dxf, "layer", "0") or "0")
                if not _in_selected(src_layer, sel):
                    continue  # filter by selected layers
                ip = ins.dxf.insert
                row = {
                    "layer": "BLOCKS", "geom": "POINT", "geometry": Point(ip.x, ip.y),
                    "src_layer": src_layer, "block_name": _safe_s(getattr(ins.dxf, "name", None) or getattr(ins, "name", None)),
                }
                try:
                    # ezdxf: Insert.attribs is a list property, not a method -- the old
                    # call ins.attribs() raised TypeError and no attribute was ever read
                    for a in (ins.attribs or []):
                        tag = _safe_s(getattr(a.dxf, "tag", None))
                        val = _safe_s(getattr(a.dxf, "text", None))
                        if tag: row[f"att_{tag}"] = val
                except Exception:
                    _log.debug("collect_annotations_and_blocks: ignored exception", exc_info=True)
                if keep_block_transform:
                    try:
                        row.update({
                            "blk_ins_x": float(ip.x), "blk_ins_y": float(ip.y), "blk_ins_z": float(getattr(ip, "z", 0.0) or 0.0),
                            "blk_rotation": float(getattr(ins.dxf, "rotation", 0.0) or 0.0),
                            "blk_sx": float(getattr(ins.dxf, "xscale", 1.0) or 1.0),
                            "blk_sy": float(getattr(ins.dxf, "yscale", 1.0) or 1.0),
                            "blk_sz": float(getattr(ins.dxf, "zscale", 1.0) or 1.0),
                        })
                    except Exception:
                        _log.debug("collect_annotations_and_blocks: ignored exception", exc_info=True)
                rows_blk.append(row)
            except Exception:
                _log.debug("collect_annotations_and_blocks: ignored exception", exc_info=True)
                continue
        if rows_blk:
            gdfb = gpd.GeoDataFrame(rows_blk, geometry="geometry", crs=f"EPSG:{src_epsg or 4326}")
            if tgt_epsg and int(tgt_epsg) != int(src_epsg or 4326):
                try: gdfb = gdfb.to_crs(epsg=int(tgt_epsg))
                except Exception:
                    _log.debug("collect_annotations_and_blocks: ignored exception", exc_info=True)
            out[("BLOCKS","POINT")] = gdfb.reset_index(drop=True)

    if not out:
        _say(on_progress, "[augment] no TEXT/MTEXT or BLOCK attributes found for selected layers")
    else:
        _say(on_progress, f"[augment] added {len(out)} extra bucket(s) from selected layers")

    return out

def attach_block_transform_to_buckets(buckets, blocks, *, on_progress=None):
    _ensure_gpd()
    if blocks is None or len(blocks) == 0:
        _say(on_progress, "[augment] no BLOCKS to attach"); return

    cols = ["blk_ins_x","blk_ins_y","blk_ins_z","blk_rotation","blk_sx","blk_sy","blk_sz"]
    have = [c for c in cols if c in blocks.columns]
    if not have:
        _say(on_progress, "[augment] BLOCKS has no transform fields"); return

    bx = blocks.geometry.x.to_numpy(); by = blocks.geometry.y.to_numpy()
    bvals = {c: (blocks[c].to_numpy() if c in blocks.columns else None) for c in cols}

    def attach_one_gdf(gdf):
        for c in cols:
            if c not in gdf.columns:
                gdf[c] = None
        reps = gdf.geometry if all(gdf.geometry.geom_type == "Point") else gdf.geometry.centroid
        gx = reps.x.to_numpy(); gy = reps.y.to_numpy()
        for i in range(len(gdf)):
            # naive nearest neighbor
            j_best = 0; d2_best = float("inf")
            x = gx[i]; y = gy[i]
            for j in range(len(blocks)):
                dx = x - bx[j]; dy = y - by[j]; d2 = dx*dx + dy*dy
                if d2 < d2_best: d2_best = d2; j_best = j
            for c in cols:
                arr = bvals.get(c)
                if arr is not None: gdf.at[i, c] = arr[j_best]

    for key, gdf in buckets.items():
        layer, geom = key
        if layer == "BLOCKS":
            continue
        try:
            attach_one_gdf(gdf)
        except Exception as ex:
            _say(on_progress, f"[augment] attach failed on {layer}: {ex}")
