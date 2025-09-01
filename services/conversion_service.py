
# -*- coding: utf-8 -*-
"""
conversion_service.py (OGR fast path)
- Per-instance BLOCK merge to BLK_* layers, with BLOCK_NAME + transform fields
- Base per-layer outputs for POINT/LINESTRING/POLYGON
- POINT layers get X/Y/Z; if OGR exposes block transform fields on points, copy Angle/Rotation/ScaleX/ScaleY/ScaleZ
- Robust BlockName detection for BLK_* (handles many field aliases and fuzzy match)
- NEW: For BLK_LN / BLK_PY, if INSERT meta missing on those features, borrow from nearest Block POINT in same layer (within tolerance)
"""

import os, hashlib, re, math
from typing import List, Dict, Any, Tuple, Optional

try:
    from osgeo import ogr, gdal, osr
    try:
        ogr.UseExceptions()
    except Exception:
        pass
    try:
        gdal.SetConfigOption("DXF_INLINE_BLOCKS", "YES")
        gdal.SetConfigOption("DXF_CLOSED_LINE_AS_POLYGON", "TRUE")
    except Exception:
        pass
except Exception:
    ogr = None
    gdal = None
    osr = None

GEOMS = ["POINT", "LINESTRING", "POLYGON"]

# ---------- small utils ----------

def _sanitize_name(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(text).strip())
    s = s.strip("_") or "layer"
    return s

def _suffix(g: str) -> str:
    return {"POINT":"_PT","LINESTRING":"_LN","POLYGON":"_PY"}.get(g, "_GX")

def _count(dxf_path: str, layer_nm: str, geom: str) -> int:
    if ogr is None: return 0
    ds = ogr.Open(dxf_path, 0)
    if ds is None: return 0
    esc = layer_nm.replace("'", "''")
    sql = f"SELECT COUNT(*) AS CNT FROM entities WHERE Layer='{esc}' AND OGR_GEOMETRY='{geom}'"
    cnt = 0
    try:
        res = ds.ExecuteSQL(sql)
        if res is not None:
            for f in res:
                val = f.GetField(0)
                if val is not None:
                    cnt = int(val)
            ds.ReleaseResultSet(res)
    except Exception:
        pass
    ds = None
    return cnt

def _ensure_prj(path: str, epsg: int):
    if not epsg or osr is None: return
    base = os.path.splitext(path)[0]
    prj = base + ".prj"
    s = osr.SpatialReference()
    if s.ImportFromEPSG(int(epsg)) == 0:
        wkt = s.ExportToWkt()
        with open(prj, "w", encoding="utf-8") as f:
            f.write(wkt)

def _compute_srs(src_epsg: int = None, tgt_epsg: int = None):
    srcSRS = f"EPSG:{src_epsg}" if src_epsg else None
    if src_epsg and tgt_epsg and tgt_epsg != src_epsg:
        dstSRS = f"EPSG:{tgt_epsg}"
    elif src_epsg:
        dstSRS = f"EPSG:{src_epsg}"
    else:
        dstSRS = None
    epsg_final = None
    if dstSRS and dstSRS.upper().startswith("EPSG:"):
        epsg_final = int(dstSRS.split(":")[1])
    elif srcSRS and srcSRS.upper().startswith("EPSG:"):
        epsg_final = int(srcSRS.split(":")[1])
    return srcSRS, dstSRS, epsg_final

def precise_convert(src_path,
                    source_epsg: int = None,
                    target_epsg: int = None,
                    include_3d: bool = False,
                    bbox_wgs84=None,
                    target_layers: List[str] = None,
                    block_mode: str = "keep-merge",
                    line_merge_tol: float = 0.0,
                    flat_dist_precise: float = 0.2,
                    fallback_explode_lines: bool = True,
                    on_progress=None) -> Dict[str, Any]:
    if isinstance(src_path, (list, tuple)):
        src_path = src_path[0]
    ez_ok = False
    try:
        import ezdxf  # noqa
        ez_ok = True
    except Exception:
        ez_ok = False
    if on_progress:
        on_progress("Using ezdxf pipeline." if ez_ok else "Using OGR fallback.")
    return {
        "_intent": "ezdxf" if ez_ok else "ogr_fallback",
        "src_path": src_path,
        "layers": target_layers or [],
        "src_epsg": source_epsg,
        "tgt_epsg": target_epsg,
        "merge_tolerance": float(line_merge_tol or 0.0),
        "block_mode": block_mode or "keep-merge",
    }

# ---------- OGR helpers ----------

def _ogr_field_names(layer) -> List[str]:
    names = []
    if layer is None: return names
    defn = layer.GetLayerDefn()
    for i in range(defn.GetFieldCount()):
        names.append(defn.GetFieldDefn(i).GetName())
    return names

def _create_or_get_field(layer, name: str, ftype):
    names = _ogr_field_names(layer)
    if name in names: return
    fd = ogr.FieldDefn(name, ftype)
    try:
        layer.CreateField(fd)
    except Exception:
        pass

def _try_get(f, keys, default=None):
    for k in keys:
        try:
            v = f.GetField(k)
            if v not in (None, ""):
                return v
        except Exception:
            continue
    return default

def _add_point_attrs_in_gpkg(ds, layer_name: str):
    """Add X/Y/Z and copy Angle/Rotation/ScaleX/ScaleY/ScaleZ if present on any feature."""
    lyr = ds.GetLayerByName(layer_name)
    if lyr is None:
        return 0
    try:
        gdef = lyr.GetGeomType()
        if gdef not in (ogr.wkbPoint, ogr.wkbPoint25D, ogr.wkbMultiPoint, ogr.wkbMultiPoint25D):
            return 0
    except Exception:
        pass
    for fld in ("X","Y","Z"):
        _create_or_get_field(lyr, fld, ogr.OFTReal)

    need_fields = set()
    features = list(lyr)
    lyr.ResetReading()
    for f in features:
        for name, keys in {
            "Angle": ["Angle","ANGLE","angle"],
            "Rotation": ["Rotation","ROTATION","rotation"],
            "ScaleX": ["ScaleX","SCALEX","scalex"],
            "ScaleY": ["ScaleY","SCALEY","scaley"],
            "ScaleZ": ["ScaleZ","SCALEZ","scalez"]
        }.items():
            if _try_get(f, keys, None) is not None:
                need_fields.add(name)
    for nf in need_fields:
        _create_or_get_field(lyr, nf, ogr.OFTReal)

    count = 0
    for f in features:
        g = f.GetGeometryRef()
        if g is None: continue
        try:
            gg = g.Centroid() if g.GetGeometryName().upper().startswith("MULTI") else g
            x, y = gg.GetX(), gg.GetY()
            try: z = gg.GetZ()
            except Exception: z = None
            f.SetField("X", float(x)); f.SetField("Y", float(y))
            if z is not None: f.SetField("Z", float(z))
            for out_name, keys in {
                "Angle": ["Angle","ANGLE","angle"],
                "Rotation": ["Rotation","ROTATION","rotation"],
                "ScaleX": ["ScaleX","SCALEX","scalex"],
                "ScaleY": ["ScaleY","SCALEY","scaley"],
                "ScaleZ": ["ScaleZ","SCALEZ","scalez"]
            }.items():
                if out_name in need_fields:
                    v = _try_get(f, keys, None)
                    if v is not None:
                        try: v = float(v)
                        except Exception: continue
                        f.SetField(out_name, v)
            lyr.SetFeature(f); count += 1
        except Exception:
            continue
    try: lyr.SyncToDisk()
    except Exception: pass
    return count

def _add_point_attrs_in_shp(shp_path: str):
    drv = ogr.GetDriverByName("ESRI Shapefile")
    ds = drv.Open(shp_path, update=1)
    if ds is None: return 0
    lyr = ds.GetLayer()
    if lyr is None: return 0
    try:
        gdef = lyr.GetGeomType()
        if gdef not in (ogr.wkbPoint, ogr.wkbPoint25D, ogr.wkbMultiPoint, ogr.wkbMultiPoint25D):
            ds = None; return 0
    except Exception:
        pass

    for fld in ("X","Y","Z"):
        _create_or_get_field(lyr, fld, ogr.OFTReal)

    need_fields = set()
    features = list(lyr)
    lyr.ResetReading()
    for f in features:
        for name, keys in {
            "Angle": ["Angle","ANGLE","angle"],
            "Rotation": ["Rotation","ROTATION","rotation"],
            "ScaleX": ["ScaleX","SCALEX","scalex"],
            "ScaleY": ["ScaleY","SCALEY","scaley"],
            "ScaleZ": ["ScaleZ","SCALEZ","scalez"]
        }.items():
            if _try_get(f, keys, None) is not None:
                need_fields.add(name)
    for nf in need_fields:
        _create_or_get_field(lyr, nf, ogr.OFTReal)

    count = 0
    for f in features:
        g = f.GetGeometryRef()
        if g is None: continue
        try:
            gg = g.Centroid() if g.GetGeometryName().upper().startswith("MULTI") else g
            x, y = gg.GetX(), gg.GetY()
            try: z = gg.GetZ()
            except Exception: z = None
            f.SetField("X", float(x)); f.SetField("Y", float(y))
            if z is not None: f.SetField("Z", float(z))
            for out_name, keys in {
                "Angle": ["Angle","ANGLE","angle"],
                "Rotation": ["Rotation","ROTATION","rotation"],
                "ScaleX": ["ScaleX","SCALEX","scalex"],
                "ScaleY": ["ScaleY","SCALEY","scaley"],
                "ScaleZ": ["ScaleZ","SCALEZ","scalez"]
            }.items():
                if out_name in need_fields:
                    v = _try_get(f, keys, None)
                    if v is not None:
                        try: v = float(v)
                        except Exception: continue
                        f.SetField(out_name, v)
            lyr.SetFeature(f); count += 1
        except Exception:
            continue
    ds = None
    return count

# ---------- BLOCK helpers (per-instance) ----------

def _ogr_block_predicate_and_keys(layer) -> Tuple[Optional[str], Optional[str], List[str], List[str]]:
    fields = _ogr_field_names(layer)

    # 1) canonical names
    lname = None
    for cand in ["BlockName","BLOCK_NAME","BLOCKNAME","BlkName","BLK_NAME","Block"]:
        if cand in fields:
            lname = cand
            break
    # 2) fuzzy: any field containing both "block"/"blk" and "name"
    if lname is None:
        for f in fields:
            low = f.lower()
            if ("block" in low or "blk" in low) and "name" in low:
                lname = f
                break

    # 3) subclasses marker
    subclass = None
    for cand in ["SubClasses","SUBCLASSES","subclasses"]:
        if cand in fields:
            subclass = cand
            break

    key_candidates = [f for f in [
        "InsertHandle","INSERT_HANDLE",
        "EntityHandle","ENTITYHANDLE",
        "Handle","HANDLE",
        "EntHandle","ENTHANDLE",
        "FeatureID","FID"
    ] if f in fields]

    transform_candidates = [f for f in [
        "RefX","RefY","RefZ","InsX","InsY","InsZ",
        "Angle","Rotation",
        "ScaleX","ScaleY","ScaleZ"
    ] if f in fields]

    where = None
    if lname:
        where = f"{lname} IS NOT NULL AND {lname} <> ''"
    elif subclass:
        where = f"{subclass} LIKE '%BlockReference%'"

    return lname, where, key_candidates, transform_candidates

def _get_num(feat, key, default=None):
    try:
        v = feat.GetField(key)
        if v in (None, ""):
            return default
        return float(v)
    except Exception:
        return default

def _best_block_name(feat, lname: Optional[str]):
    # If lname present, use it
    if lname:
        try:
            v = feat.GetField(lname)
            if v not in (None, ""):
                return str(v)
        except Exception:
            pass
    # Otherwise search all fields for something like block name
    try:
        defn = feat.GetDefnRef()
        for i in range(defn.GetFieldCount()):
            nm = defn.GetFieldDefn(i).GetName()
            low = nm.lower()
            if ("block" in low or "blk" in low) and "name" in low:
                v = feat.GetField(i)
                if v not in (None, ""):
                    return str(v)
    except Exception:
        pass
    # Fallback: empty string
    return ""

def _inst_key(feat, key_fields: List[str], fallback_fields: List[str]) -> str:
    for k in key_fields:
        try:
            v = feat.GetField(k)
            if v not in (None, ""):
                return f"{k}:{v}"
        except Exception:
            continue
    parts = []
    for k in fallback_fields:
        try:
            v = feat.GetField(k)
        except Exception:
            v = None
        parts.append(str(v))
    data = "|".join(parts).encode("utf-8")
    return "TFM:" + hashlib.md5(data).hexdigest()

def _pt_xy(g) -> Optional[Tuple[float,float]]:
    try:
        if g.GetGeometryName().upper().startswith("MULTI"):
            gg = g.Centroid()
        else:
            gg = g
        return gg.GetX(), gg.GetY()
    except Exception:
        return None

def _dist2(a: Tuple[float,float], b: Tuple[float,float]) -> float:
    dx = a[0]-b[0]; dy = a[1]-b[1]
    return dx*dx + dy*dy

def _ogr_collect_blocks_per_instance(dxf_path: str, layer_name: str, tol: float = 0.0):
    """Return dict: {kind: {instance_id: {'name': blkname, 'geoms': [geom,...], 'meta': {...}}}}.
       For LINESTRING/POLYGON missing INSERT meta, we borrow meta from nearest Block POINT of same layer within tol.
    """
    res = {"POINT": {}, "LINESTRING": {}, "POLYGON": {}}
    if ogr is None or not os.path.exists(dxf_path):
        return res
    ds = ogr.Open(dxf_path, 0)
    if ds is None:
        return res
    src = ds.GetLayerByName("entities") or (ds.GetLayer(0) if ds.GetLayerCount() else None)
    if src is None:
        ds = None; return res

    lname, where_blk, key_fields, tfm_fields = _ogr_block_predicate_and_keys(src)
    if where_blk is None:
        ds = None; return res

    esc_layer = layer_name.replace("'", "''")

    # Phase A: collect BLOCK POINTs for borrowing meta later
    pt_meta = []  # list of (xy, inst_id, name, meta_dict)
    pt_sql = f"SELECT * FROM entities WHERE Layer='{esc_layer}' AND OGR_GEOMETRY='POINT' AND {where_blk}"
    try:
        lyr_pts = ds.ExecuteSQL(pt_sql, dialect="OGRSQL")
    except Exception:
        lyr_pts = None
    if lyr_pts is not None:
        for f in lyr_pts:
            g = f.GetGeometryRef()
            xy = _pt_xy(g)
            if xy is None:
                continue
            raw_name = _best_block_name(f, lname)
            inst_id = _inst_key(f, key_fields, tfm_fields)
            meta = {
                "BLK_X": (_get_num(f, "InsX", None) or _get_num(f, "RefX", None) or xy[0]),
                "BLK_Y": (_get_num(f, "InsY", None) or _get_num(f, "RefY", None) or xy[1]),
                "BLK_Z": (_get_num(f, "InsZ", None) or _get_num(f, "RefZ", None)),
                "BLK_ROT": (_get_num(f, "Angle", None) or _get_num(f, "Rotation", None)),
                "BLK_SX": _get_num(f, "ScaleX", None),
                "BLK_SY": _get_num(f, "ScaleY", None),
                "BLK_SZ": _get_num(f, "ScaleZ", None),
            }
            pt_meta.append((xy, inst_id, raw_name, meta))
        try:
            ds.ReleaseResultSet(lyr_pts)
        except Exception:
            pass

    # Phase B: iterate all block features on the layer
    sql = f"SELECT * FROM entities WHERE Layer='{esc_layer}' AND {where_blk}"
    try:
        lyr = ds.ExecuteSQL(sql, dialect="OGRSQL")
    except Exception:
        lyr = None
    if lyr is None:
        ds = None; return res

    tol2 = (tol or 0.0) ** 2

    def _snap(g):
        try:
            c = g.Clone()
            if tol and hasattr(c, "SnapToGrid"):
                return c.SnapToGrid(tol)
            return c
        except Exception:
            return g.Clone()

    def _ensure_entry(bucket: Dict, inst_id: str, blkname: str, meta: Dict[str,Any]):
        entry = bucket.setdefault(inst_id, {"name": blkname or "", "geoms": [], "meta": {}})
        if not entry["name"]:
            entry["name"] = blkname or ""
        if meta and not entry["meta"]:
            entry["meta"] = dict(meta)
        return entry

    def _nearest_point_meta(xy: Tuple[float,float]):
        if not pt_meta:
            return None
        best = None
        best_d2 = None
        for xy2, inst2, name2, meta2 in pt_meta:
            d2 = _dist2(xy, xy2)
            if (best_d2 is None) or (d2 < best_d2):
                best_d2 = d2
                best = (inst2, name2, meta2)
        if best is None:
            return None
        if tol2 and best_d2 is not None and best_d2 > tol2:
            return None
        return best  # (inst_id, blk_name, meta)

    def _append(kind: str, inst_id: str, blkname: str, geom, meta: Dict[str,Any]):
        if geom is None:
            return
        entry = _ensure_entry(res[kind], inst_id, blkname, meta)
        entry["geoms"].append(_snap(geom))

    for f in lyr:
        g = f.GetGeometryRef()
        if g is None: 
            continue
        gname = g.GetGeometryName().upper() if hasattr(g, "GetGeometryName") else ""
        raw_name = _best_block_name(f, lname)
        inst_id = _inst_key(f, key_fields, tfm_fields)
        # try get meta directly on this feat
        meta_here = {
            "BLK_X": (_get_num(f, "InsX", None) or _get_num(f, "RefX", None)),
            "BLK_Y": (_get_num(f, "InsY", None) or _get_num(f, "RefY", None)),
            "BLK_Z": (_get_num(f, "InsZ", None) or _get_num(f, "RefZ", None)),
            "BLK_ROT": (_get_num(f, "Angle", None) or _get_num(f, "Rotation", None)),
            "BLK_SX": _get_num(f, "ScaleX", None),
            "BLK_SY": _get_num(f, "ScaleY", None),
            "BLK_SZ": _get_num(f, "ScaleZ", None),
        }
        has_meta = any(v is not None for v in meta_here.values())

        # If no meta on this feat and it's not a point, borrow from nearest block point
        if not has_meta and (not gname.startswith("POINT")):
            xy = _pt_xy(g.Centroid() if gname.startswith("MULTI") or gname.startswith("GEOMETRYCOLLECTION") else g)
            if xy is not None:
                borrowed = _nearest_point_meta(xy)
                if borrowed is not None:
                    inst_id, raw_name_b, meta_b = borrowed
                    if raw_name_b and not raw_name:
                        raw_name = raw_name_b
                    meta_here = dict(meta_b)
                    has_meta = True

        # classify
        if gname.startswith("GEOMETRYCOLLECTION") or gname.startswith("MULTI"):
            try:
                n = g.GetGeometryCount()
            except Exception:
                n = 0
            for i in range(n):
                gi = g.GetGeometryRef(i)
                if gi is None: 
                    continue
                # recurse classification for parts
                gname_i = gi.GetGeometryName().upper() if hasattr(gi, "GetGeometryName") else ""
                if gname_i.startswith("POLY"):
                    _append("POLYGON", inst_id, raw_name, gi, meta_here)
                elif "LINESTRING" in gname_i:
                    _append("LINESTRING", inst_id, raw_name, gi, meta_here)
                elif "POINT" in gname_i:
                    _append("POINT", inst_id, raw_name, gi, meta_here)
            continue

        if gname.startswith("POLY"):
            _append("POLYGON", inst_id, raw_name, g, meta_here)
        elif "LINESTRING" in gname:
            _append("LINESTRING", inst_id, raw_name, g, meta_here)
        elif "POINT" in gname:
            _append("POINT", inst_id, raw_name, g, meta_here)

    try:
        ds.ReleaseResultSet(lyr)
    except Exception:
        pass
    ds = None
    return res

def _merge_geoms(geoms: List[Any], kind: str):
    if not geoms: return None
    if kind == "LINESTRING":
        multi = ogr.Geometry(ogr.wkbMultiLineString)
        for g in geoms: multi.AddGeometry(g)
        try:
            merged = multi.LineMerge()
            if merged and merged.GetGeometryName().upper() == "LINESTRING":
                out = ogr.Geometry(ogr.wkbMultiLineString); out.AddGeometry(merged); return out
            return merged or multi
        except Exception:
            return multi
    elif kind == "POLYGON":
        multi = ogr.Geometry(ogr.wkbMultiPolygon)
        for g in geoms:
            if g.GetGeometryName().upper().startswith("POLY"):
                multi.AddGeometry(g)
        try:
            dissolved = multi.UnionCascaded()
            if dissolved and dissolved.GetGeometryName().upper() == "POLYGON":
                out = ogr.Geometry(ogr.wkbMultiPolygon); out.AddGeometry(dissolved); return out
            return dissolved or multi
        except Exception:
            return multi
    else:  # POINT
        out = ogr.Geometry(ogr.wkbMultiPoint)
        seen = set()
        for g in geoms:
            try:
                x, y = g.GetX(), g.GetY()
                key = (x, y)
                if key in seen: continue
                seen.add(key)
            except Exception:
                pass
            out.AddGeometry(g)
        return out

def _write_blk_outputs(out_path: str, driver: str, layer_name: str,
                       grouped: Dict[str, Dict[str, Dict[str, Any]]],
                       epsg_final: int = None) -> List[Dict[str, Any]]:
    written = []
    drv = driver.upper()
    for kind in ["POINT","LINESTRING","POLYGON"]:
        per_inst = grouped.get(kind) or {}
        if not per_inst:
            continue
        safe_layer = _sanitize_name(layer_name) + "_BLK" + _suffix(kind)
        gtype = {"POINT": ogr.wkbMultiPoint, "LINESTRING": ogr.wkbMultiLineString, "POLYGON": ogr.wkbMultiPolygon}[kind]
        if drv == "GPKG":
            ds = ogr.Open(out_path, update=1)
            if ds is None:
                ds = gdal.GetDriverByName("GPKG").Create(out_path, 0, 0, 0, gdal.GDT_Unknown)
            try:
                ds.DeleteLayer(safe_layer)
            except Exception:
                pass
            lyr = ds.CreateLayer(safe_layer, srs=None, geom_type=gtype)
            lyr.CreateField(ogr.FieldDefn("IS_BLOCK", ogr.OFTInteger))
            f_id = ogr.FieldDefn("BLK_ID", ogr.OFTString); f_id.SetWidth(64); lyr.CreateField(f_id)
            f_nm = ogr.FieldDefn("BLOCK_NAME", ogr.OFTString); f_nm.SetWidth(80); lyr.CreateField(f_nm)
            for fld in ["BLK_X","BLK_Y","BLK_Z","BLK_ROT","BLK_SX","BLK_SY","BLK_SZ"]:
                lyr.CreateField(ogr.FieldDefn(fld, ogr.OFTReal))
            defn = lyr.GetLayerDefn()
            count = 0
            for inst_id, entry in per_inst.items():
                mg = _merge_geoms(entry.get("geoms") or [], kind)
                if mg is None: continue
                feat = ogr.Feature(defn)
                feat.SetField("IS_BLOCK", 1)
                feat.SetField("BLK_ID", inst_id)
                feat.SetField("BLOCK_NAME", entry.get("name") or "")
                meta = entry.get("meta") or {}
                for fld in ["BLK_X","BLK_Y","BLK_Z","BLK_ROT","BLK_SX","BLK_SY","BLK_SZ"]:
                    v = meta.get(fld, None)
                    if v is not None: feat.SetField(fld, float(v))
                feat.SetGeometry(mg)
                lyr.CreateFeature(feat); count += 1
            ds = None
            if count > 0:
                written.append({"layer": safe_layer, "path": out_path, "count": count})
        else:
            shp = os.path.join(out_path, safe_layer + ".shp")
            base = os.path.splitext(shp)[0]
            for ext in (".shp",".dbf",".shx",".prj",".cpg"):
                p = base+ext
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            drv_shp = ogr.GetDriverByName("ESRI Shapefile")
            ds = drv_shp.CreateDataSource(shp)
            lyr = ds.CreateLayer(os.path.basename(base), srs=None, geom_type=gtype)
            lyr.CreateField(ogr.FieldDefn("IS_BLOCK", ogr.OFTInteger))
            f_id = ogr.FieldDefn("BLK_ID", ogr.OFTString); f_id.SetWidth(64); lyr.CreateField(f_id)
            f_nm = ogr.FieldDefn("BLOCK_NAME", ogr.OFTString); f_nm.SetWidth(80); lyr.CreateField(f_nm)
            for fld in ["BLK_X","BLK_Y","BLK_Z","BLK_ROT","BLK_SX","BLK_SY","BLK_SZ"]:
                lyr.CreateField(ogr.FieldDefn(fld, ogr.OFTReal))
            defn = lyr.GetLayerDefn()
            count = 0
            for inst_id, entry in per_inst.items():
                mg = _merge_geoms(entry.get("geoms") or [], kind)
                if mg is None: continue
                feat = ogr.Feature(defn)
                feat.SetField("IS_BLOCK", 1)
                feat.SetField("BLK_ID", inst_id)
                feat.SetField("BLOCK_NAME", entry.get("name") or "")
                meta = entry.get("meta") or {}
                for fld in ["BLK_X","BLK_Y","BLK_Z","BLK_ROT","BLK_SX","BLK_SY","BLK_SZ"]:
                    v = meta.get(fld, None)
                    if v is not None: feat.SetField(fld, float(v))
                feat.SetGeometry(mg)
                lyr.CreateFeature(feat); count += 1
            ds = None
            if count > 0:
                if epsg_final: _ensure_prj(shp, epsg_final)
                written.append({"layer": safe_layer, "path": shp, "count": count})
            else:
                for ext in (".shp",".dbf",".shx",".prj",".cpg"):
                    p = base+ext
                    if os.path.exists(p):
                        try: os.remove(p)
                        except Exception: pass
    return written

# ---------- main export ----------

def _export_with_ogr(intent: Dict[str, Any], out_path: str, driver: str, overwrite: bool) -> List[Dict[str, Any]]:
    dxf = intent["src_path"]
    layers = intent.get("layers") or []
    src_epsg = intent.get("src_epsg")
    tgt_epsg = intent.get("tgt_epsg")
    merge_tol = float(intent.get("merge_tolerance") or 0.0)
    block_mode = intent.get("block_mode") or "keep-merge"

    if ogr is None or gdal is None:
        raise RuntimeError("GDAL/OGR not available.")

    srcSRS, dstSRS, epsg_final = _compute_srs(src_epsg, tgt_epsg)
    written = []
    drv = driver.upper()

    # Base geometries
    if drv == "GPKG":
        if overwrite and os.path.exists(out_path):
            try: os.remove(out_path)
            except Exception: pass
        for nm in layers:
            esc = nm.replace("'", "''")
            for g in GEOMS:
                if _count(dxf, nm, g) == 0:
                    continue
                safe = _sanitize_name(nm) + _suffix(g)
                sql = f"SELECT * FROM entities WHERE Layer='{esc}' AND OGR_GEOMETRY='{g}'"
                kwargs = dict(format="GPKG", accessMode="append", SQLStatement=sql, SQLDialect="OGRSQL",
                              layerName=safe, datasetCreationOptions=["GEOMETRY_NAME=geometry"], skipFailures=True)
                if srcSRS: kwargs["srcSRS"] = srcSRS
                if dstSRS: kwargs["dstSRS"] = dstSRS
                opts = gdal.VectorTranslateOptions(**kwargs)
                res = gdal.VectorTranslate(out_path, dxf, options=opts)
                if res:
                    written.append({"layer": safe, "path": out_path, "count": _count(dxf, nm, g)})
        # POINT attrs
        ds = ogr.Open(out_path, update=1)
        if ds is not None:
            for nm in layers:
                pt_name = _sanitize_name(nm) + "_PT"
                try:
                    _add_point_attrs_in_gpkg(ds, pt_name)
                except Exception:
                    pass
            ds = None
    else:
        os.makedirs(out_path, exist_ok=True)
        shp_to_patch = []
        for nm in layers:
            esc = nm.replace("'", "''")
            for g in GEOMS:
                if _count(dxf, nm, g) == 0:
                    continue
                safe = _sanitize_name(nm) + _suffix(g)
                shp = os.path.join(out_path, safe + ".shp")
                if overwrite:
                    base = os.path.splitext(shp)[0]
                    for ext in (".shp",".dbf",".shx",".prj",".cpg"):
                        p = base+ext
                        if os.path.exists(p):
                            try: os.remove(p)
                            except Exception: pass
                sql = f"SELECT * FROM entities WHERE Layer='{esc}' AND OGR_GEOMETRY='{g}'"
                kwargs = dict(format="ESRI Shapefile", SQLStatement=sql, SQLDialect="OGRSQL",
                              layerName=safe, skipFailures=True)
                if srcSRS: kwargs["srcSRS"] = srcSRS
                if dstSRS: kwargs["dstSRS"] = dstSRS
                opts = gdal.VectorTranslateOptions(**kwargs)
                res = gdal.VectorTranslate(shp, dxf, options=opts)
                if res:
                    try:
                        if epsg_final: _ensure_prj(shp, epsg_final)
                    except Exception:
                        pass
                    written.append({"layer": safe, "path": shp, "count": _count(dxf, nm, g)})
                    if g == "POINT":
                        shp_to_patch.append(shp)
        for shp in shp_to_patch:
            try: _add_point_attrs_in_shp(shp)
            except Exception: pass

    # BLK_* per-instance
    if block_mode == "keep-merge":
        for nm in layers:
            grouped = _ogr_collect_blocks_per_instance(dxf, nm, tol=merge_tol)
            if not any(grouped[k] for k in grouped):
                continue
            blk_written = _write_blk_outputs(out_path, drv, nm, grouped, epsg_final=epsg_final)
            written.extend(blk_written)

    return written

def write_outputs(buckets: Dict[str, Any], out_path: str, driver: str, overwrite: bool, on_progress=None):
    intent = buckets or []
    if isinstance(intent, dict):
        intent["_intent"] = "ogr_fallback"
    else:
        intent = {"_intent":"ogr_fallback", "src_path": intent, "layers": []}
    if on_progress: on_progress("Writing with OGR (fast merge) ...")
    return _export_with_ogr(intent, out_path, driver, overwrite)
