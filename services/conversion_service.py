# -*- coding: utf-8 -*-


from __future__ import annotations

import re
from typing import Tuple, Dict, List, Optional, Callable

ProgressCB = Optional[Callable[[str], None]]


# -------------------------
# Path coercion
# -------------------------
def _coerce_paths(src_path, dxf_paths_kw=None) -> List[str]:
    """
    Normalize incoming DXF path(s), being tolerant to multiple forms:
      - 'C:/a.dxf' (string)
      - ['C:/a.dxf', 'D:/b.dxf'] (list/tuple)
      - "['C:/a.dxf']" (stringified list)
      - dxf_paths_kw: same variants

    Returns a de-duplicated, order-preserving list of string paths.
    """
    import ast
    out: List[str] = []

    def _eat(x):
        if not x:
            return
        if isinstance(x, (list, tuple)):
            for p in x:
                if p:
                    out.append(str(p))
        elif isinstance(x, str):
            s = x.strip()
            if s.startswith("[") and s.endswith("]"):
                # Try to parse stringified list safely
                try:
                    arr = ast.literal_eval(s)
                    if isinstance(arr, (list, tuple)):
                        for p in arr:
                            if p:
                                out.append(str(p))
                        return
                except Exception:
                    pass
                # Fallback: coarse split/strip
                s2 = s.strip("[]").strip().strip("'\"")
                if s2:
                    out.append(s2)
            else:
                out.append(s)

    _eat(dxf_paths_kw)
    _eat(src_path)

    # De-duplicate while preserving order
    seen = set()
    norm = []
    for p in out:
        if p not in seen:
            seen.add(p)
            norm.append(p)
    return norm


# -------------------------
# Geometry & flattening helpers
# -------------------------
def _sanitize_filename(name: str) -> str:
    """Safe file/layer name for Windows and OGR drivers."""
    import re as _re
    if name is None:
        name = "layer"
    s = str(name)
    s = _re.sub(r"[^A-Za-z0-9 _.\-]+", "_", s).strip(" .")
    if not s:
        s = "layer"
    reserved = {"CON","PRN","AUX","NUL",
                "COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9",
                "LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"}
    if s.upper() in reserved:
        s = f"_{s}_"
    return s[:100]


def _coords_to_geom(coords: List[tuple]):
    """Turn a coordinate sequence into a Shapely geometry and report its type."""
    from shapely.geometry import Point, LineString, Polygon
    xy = [(x, y) for x, y, _ in coords]
    if not xy:
        return None, None
    if len(xy) == 1:
        return "POINT", Point(xy[0])
    if xy[0] == xy[-1] and len(xy) >= 4:
        return "POLYGON", Polygon(xy)
    return "LINE", LineString(xy)


def _flatten_with_path(e, dist: float) -> List[tuple]:
    """Flatten curves (SPLINE/ELLIPSE/ARC) using ezdxf.path."""
    from ezdxf import path as ezpath
    p = ezpath.make_path(e)
    return [(v.x, v.y, 0.0) for v in p.flattening(distance=dist)]


def _fallback_polyline(e, include_3d: bool) -> List[tuple]:
    """Fallback flattening for polylines."""
    pts: List[tuple] = []
    if hasattr(e, "get_points"):
        for v in e.get_points():
            pts.append((v[0], v[1], 0.0))
    else:
        for v in e:
            loc = v.dxf.location
            pts.append((loc.x, loc.y, loc.z if include_3d else 0.0))
    return pts


def _fallback_circle(e, dist: float) -> List[tuple]:
    """Approximate circle by polyline."""
    import math
    c = e.dxf.center
    r = float(e.dxf.radius)
    segs = max(24, int(6.28318530718 / max(dist, 0.1)))
    return [(c.x + r * math.cos(2 * math.pi * i / segs),
             c.y + r * math.sin(2 * math.pi * i / segs), 0.0)
            for i in range(segs + 1)]


def _fallback_arc(e, dist: float) -> List[tuple]:
    """Approximate arc by polyline."""
    import math
    c = e.dxf.center
    r = float(e.dxf.radius)
    a1 = math.radians(float(e.dxf.start_angle))
    a2 = math.radians(float(e.dxf.end_angle))
    if a2 < a1:
        a1, a2 = a2, a1
    steps = max(16, int((a2 - a1) / max(dist, 0.05)))
    return [(c.x + r * math.cos(a1 + (a2 - a1) * i / steps),
             c.y + r * math.sin(a1 + (a2 - a1) * i / steps), 0.0)
            for i in range(steps + 1)]


def _flatten_hatch_rings(e) -> List[List[tuple]]:
    """Extract HATCH boundaries (rings) as coordinate lists."""
    rings: List[List[tuple]] = []
    try:
        for p in e.paths.polygons:  # type: ignore
            ring = [(pt[0], pt[1], 0.0) for pt in p]
            if ring and ring[0] != ring[-1]:
                ring.append(ring[0])
            if ring:
                rings.append(ring)
    except Exception:
        pass
    try:
        if not rings:
            for path in e.paths:
                ring = []
                for edge in path.edges:
                    if hasattr(edge, "start"):
                        s = edge.start
                        ring.append((s[0], s[1], 0.0))
                if ring and ring[0] != ring[-1]:
                    ring.append(ring[0])
                if ring:
                    rings.append(ring)
    except Exception:
        pass
    return rings


def _precise_rows_from_entity(e, layer, include_3d, dist) -> List[dict]:
    """
    Convert a single DXF entity to one or more feature rows:
    returns dicts with keys: layer, geom ('POINT'/'LINE'/'POLYGON'), geometry (shapely).
    """
    from shapely.geometry import Point
    rows: List[dict] = []
    t = e.dxftype()
    if t in {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE"}:
        try:
            pts = _flatten_with_path(e, dist)
        except Exception:
            if t == "LINE":
                s, ed = e.dxf.start, e.dxf.end
                pts = [(s.x, s.y, s.z if include_3d else 0.0),
                       (ed.x, ed.y, ed.z if include_3d else 0.0)]
            elif t in {"LWPOLYLINE", "POLYLINE"}:
                pts = _fallback_polyline(e, include_3d)
            elif t == "CIRCLE":
                pts = _fallback_circle(e, dist)
            elif t == "ARC":
                pts = _fallback_arc(e, dist)
            else:
                pts = []
        if pts:
            gtype, geom = _coords_to_geom(pts)
            if gtype and geom:
                rows.append({"layer": layer, "geom": gtype, "geometry": geom})
    elif t == "POINT":
        p = e.dxf.location
        rows.append({"layer": layer, "geom": "POINT", "geometry": Point(p.x, p.y)})
    elif t == "HATCH":
        for ring in _flatten_hatch_rings(e):
            gtype, geom = _coords_to_geom(ring)
            if gtype and geom:
                rows.append({"layer": layer, "geom": gtype, "geometry": geom})
    elif t in {"3DFACE", "THREE_D_FACE", "SOLID"}:
        # Treat as polygon face
        try:
            v0, v1, v2, v3 = e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx2, e.dxf.vtx3
            ring = [(v0.x, v0.y, 0.0), (v1.x, v1.y, 0.0),
                    (v2.x, v2.y, 0.0), (v3.x, v3.y, 0.0), (v0.x, v0.y, 0.0)]
            gtype, geom = _coords_to_geom(ring)
            if gtype and geom:
                rows.append({"layer": layer, "geom": gtype, "geometry": geom})
        except Exception:
            pass
    return rows


# -------------------------
# BLOCK metadata extraction
# -------------------------
def _extract_block_meta(e) -> dict:
    """
    Extract metadata from an INSERT (block):
      - block_name
      - BLK_X, BLK_Y, BLK_Z (insert location)
      - BLK_ROT (rotation, degrees)
      - BLK_SX, BLK_SY, BLK_SZ (scale factors)
      - ATT_* (block attribute text, keys derived from ATTRIB tags)
    """
    meta = {}
    try:
        bname = (getattr(e.dxf, "name", None) or getattr(e, "name", None) or "")
        meta["block_name"] = str(bname)
    except Exception:
        meta["block_name"] = ""

    # Insert point / rotation / scale
    try:
        ip = e.dxf.insert
        meta["BLK_X"] = float(getattr(ip, "x", 0.0))
        meta["BLK_Y"] = float(getattr(ip, "y", 0.0))
        meta["BLK_Z"] = float(getattr(ip, "z", 0.0))
    except Exception:
        pass
    for k_src, k_out in [("rotation","BLK_ROT"), ("xscale","BLK_SX"),
                         ("yscale","BLK_SY"), ("zscale","BLK_SZ")]:
        try:
            v = getattr(e.dxf, k_src, None)
            if v is not None:
                meta[k_out] = float(v)
        except Exception:
            pass

    # Block attributes (ATTRIB) → ATT_*
    try:
        for a in getattr(e, "attribs", []) or []:
            try:
                tag = str(a.dxf.tag).strip()
                txt = a.dxf.text
                if not tag:
                    continue
                key = "ATT_" + re.sub(r"[^0-9A-Za-z_]+", "_", tag.upper())
                meta[key] = "" if txt is None else str(txt)
            except Exception:
                continue
    except Exception:
        pass
    return meta


# -------------------------
# Line merging helpers for keep-merge strategy
# -------------------------
def _grid_snap_lines(lines, tol: float):
    """Snap only endpoints to grid (tolerance) to improve later merges."""
    from shapely.geometry import LineString
    if tol <= 0 or not lines:
        return lines

    def q(x): return round(x / tol) * tol

    out = []
    for ls in lines:
        try:
            coords = list(ls.coords)
            if len(coords) < 2:
                continue
            head = (q(coords[0][0]), q(coords[0][1]))
            tail = (q(coords[-1][0]), q(coords[-1][1]))
            mid = coords[1:-1]
            new = [head] + mid + [tail]
            # compress duplicates
            clean = [new[0]]
            for p in new[1:]:
                if p != clean[-1]:
                    clean.append(p)
            if len(clean) >= 2:
                ls2 = LineString(clean)
                if ls2.length > 0:
                    out.append(ls2)
        except Exception:
            continue
    return out


def _merge_lines_robust(lines, tol: float, say=lambda m: None):
    """Multi-step robust merge using unary_union, snap, and boundary buffer."""
    from shapely.ops import unary_union, linemerge
    from shapely import snap as shp_snap
    if not lines:
        return None
    try:
        L1 = _grid_snap_lines(lines, tol)
        if not L1:
            return None
        u1 = unary_union(L1)
        m1 = linemerge(u1)
        if m1 and not getattr(m1, "is_empty", False):
            return m1
        # Try snapping union to itself
        try:
            u2 = unary_union(L1)
            s2 = shp_snap(u2, u2, tol * 1.0)
            m2 = linemerge(s2)
            if m2 and not getattr(m2, "is_empty", False):
                return m2
        except Exception as ex:
            say(f"[merge] snap failed: {ex}")
        # Try boundary of small buffer
        try:
            b = unary_union(L1).buffer(tol * 0.5, join_style=2)
            bdry = b.boundary
            m3 = linemerge(unary_union(bdry))
            if m3 and not getattr(m3, "is_empty", False):
                return m3
        except Exception as ex:
            say(f"[merge] buffer/boundary failed: {ex}")
    except Exception as ex:
        say(f"[merge] exception: {ex}")
    return None


def _merge_lines_graph(lines, tol: float):
    """Graph-based greedy path stitcher for medium-size inputs."""
    from shapely.geometry import LineString, MultiLineString
    if not lines:
        return None

    def q(x): return round(x / tol) * tol if tol > 0 else x

    endpoints = []
    for ls in lines:
        try:
            coords = list(ls.coords)
            if len(coords) < 2:
                continue
            a = (q(coords[0][0]), q(coords[0][1]))
            b = (q(coords[-1][0]), q(coords[-1][1]))
            endpoints.append((a, b, coords))
        except Exception:
            continue

    if not endpoints:
        return None

    from collections import defaultdict as _dd
    node_deg = _dd(int)
    adj = _dd(list)
    edges = []
    for i, (a, b, coords) in enumerate(endpoints):
        edges.append((a, b, coords))
        adj[a].append(i); adj[b].append(i)
        node_deg[a] += 1; node_deg[b] += 1
    used_edge = [False]*len(edges)

    def build_path(start_node):
        path = []
        cur = start_node
        while True:
            nxt = None
            for ei in adj[cur]:
                if not used_edge[ei]:
                    nxt = ei; break
            if nxt is None: break
            used_edge[nxt] = True
            a, b, coords = edges[nxt]
            if a == cur:
                seg = coords; other = b
            else:
                seg = list(reversed(coords)); other = a
            if not path:
                path.extend(seg)
            else:
                path.extend(seg[1:] if path[-1] == seg[0] else seg)
            if node_deg[other] != 2:
                break
            cur = other
        return path

    merged = []
    for node, deg in node_deg.items():
        if deg != 2:
            while any(not used_edge[e] for e in adj[node]):
                coords = build_path(node)
                if coords and len(coords) >= 2:
                    try: merged.append(LineString(coords))
                    except: pass

    for ei, used in enumerate(used_edge):
        if not used:
            a, b, coords0 = edges[ei]
            used_edge[ei] = True
            cur_coords = list(coords0); cur_node = b
            while True:
                nxt = None
                for ej in adj[cur_node]:
                    if not used_edge[ej]:
                        nxt = ej; break
                if nxt is None: break
                used_edge[nxt] = True
                a2, b2, coords2 = edges[nxt]
                if a2 == cur_node:
                    seg = coords2; cur_node = b2
                else:
                    seg = list(reversed(coords2)); cur_node = a2
                cur_coords.extend(seg[1:] if cur_coords[-1] == seg[0] else seg)
                if node_deg[cur_node] != 2:
                    break
            if len(cur_coords) >= 2:
                try: merged.append(LineString(cur_coords))
                except: pass

    if not merged:
        return None
    return merged[0] if len(merged) == 1 else MultiLineString([ls for ls in merged if ls.length > 0])


def _extract_lineal(geom):
    """Extract a pure (Multi)LineString from arbitrary geometry collections."""
    from shapely.geometry import LineString, MultiLineString, GeometryCollection
    from shapely.ops import unary_union, linemerge
    if geom is None:
        return None
    if isinstance(geom, (LineString, MultiLineString)):
        return geom if not geom.is_empty else None
    if isinstance(geom, GeometryCollection):
        lines = []
        for g in geom.geoms:
            if isinstance(g, (LineString, MultiLineString)) and not g.is_empty:
                lines.append(g)
        if not lines:
            return None
        try:
            m = linemerge(unary_union(lines))
            return m if (m and not m.is_empty) else None
        except Exception:
            flat = []
            for g in lines:
                if isinstance(g, LineString) and not g.is_empty:
                    flat.append(g)
                elif isinstance(g, MultiLineString):
                    flat.extend([ls for ls in g.geoms if not ls.is_empty])
            from shapely.geometry import MultiLineString as MLS
            return MLS(flat) if flat else None
    return None


def _normalize_bucket_geoms(layer_geom_key: tuple, gdf):
    """
    Ensure the GeoDataFrame geometries match the group geometry type key,
    dropping empties or incompatible shapes.
    """
    from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiPolygon, MultiLineString
    want = (layer_geom_key[1] or "").upper()
    if want == "LINE":
        fixed = []
        for g in gdf.geometry:
            lg = _extract_lineal(g)
            fixed.append(lg)
        gdf = gdf.copy()
        gdf["geometry"] = fixed
        gdf = gdf[~gdf.geometry.isna() & ~gdf.geometry.is_empty]
        return gdf
    elif want == "POLYGON":
        mask = gdf.geometry.apply(lambda g: isinstance(g, (Polygon, MultiPolygon)) and g and not g.is_empty)
        return gdf.loc[mask].copy()
    elif want == "POINT":
        mask = gdf.geometry.apply(lambda g: isinstance(g, (Point, MultiPoint)) and g and not g.is_empty)
        return gdf.loc[mask].copy()
    return gdf


# -------------------------
# Core conversion (v1.1-style via ezdxf)
# -------------------------
def _precise_convert_ezdxf(
    dxf_paths: List[str],
    *,
    source_epsg: int = 3826,
    target_epsg: Optional[int] = None,
    include_3d: bool = False,
    flat_dist_precise: float = 0.2,
    target_layers: Optional[List[str]] = None,
    bbox_wgs84: Optional[Tuple[float, float, float, float]] = None,
    on_progress: ProgressCB = None,
    block_mode: str = "keep-merge",
    line_merge_tol: float = 0.2,
    keep_merge_small_limit: int = 2_000,
    keep_merge_medium_limit: int = 20_000,
    keep_merge_time_soft_ms: int = 2_000,
) -> Dict[Tuple[str, str], "gpd.GeoDataFrame"]:
    """
    Convert one or more DXF files into grouped GeoDataFrames keyed by (layer, geom-type).
    INSERT entities are expanded with three strategies:
      - keep-merge: collect parts, attempt line/polygon merges
      - graph: faster heuristic merging
      - explode: as a last resort, emit each part individually
    """
    import time, ezdxf, geopandas as gpd
    from shapely.geometry import Point

    def say(m):
        if on_progress:
            try: on_progress(m)
            except: pass

    sel_layers = set(target_layers) if target_layers else None
    say(f"[convert] srcEPSG={source_epsg} tgtEPSG={target_epsg} bbox={bbox_wgs84} include_3d={include_3d} block_mode={block_mode}")

    rows: List[dict] = []
    for path in dxf_paths:
        try:
            say(f"[convert] read: {path}")
            doc = ezdxf.readfile(path)
            msp = doc.modelspace()
        except Exception as ex:
            say(f"[error] read failed: {ex}")
            continue

        count = 0
        ins_count = 0
        for e in msp:
            count += 1
            if count % 1000 == 0:
                say(f"[convert] processing… {count} entities")

            try:
                t = e.dxftype()
                layer = getattr(e.dxf, "layer", "0") or "0"
                if sel_layers and layer not in sel_layers:
                    continue

                if t == "INSERT":
                    ins_count += 1
                    if ins_count % 50 == 0:
                        say(f"[convert] …INSERT expanded: {ins_count}")

                    meta = _extract_block_meta(e)
                    mode = (block_mode or "explode").lower().strip()

                    if mode in ("keep-merge", "keep-merge-per"):
                        from shapely.geometry import LineString, Polygon
                        from shapely.ops import unary_union
                        polys: List[Polygon] = []
                        lines: List[LineString] = []
                        t0 = time.perf_counter()
                        segs_seen = 0

                        def _push_line_from_pts(pts):
                            """Collect a line-segment from a list of points."""
                            nonlocal segs_seen
                            if not pts:
                                return
                            xy = [(x, y) for x, y, _ in pts]
                            if len(xy) >= 2:
                                if xy[0] == xy[-1] and len(xy) >= 3:
                                    xy = xy[:-1]
                                if len(xy) >= 2:
                                    try:
                                        lines.append(LineString(xy))
                                        segs_seen += 1
                                    except Exception:
                                        pass
                            if segs_seen and segs_seen % 5000 == 0:
                                say(f"[keep] block={meta.get('block_name','')} collected {segs_seen} segments…")

                        # Collect block virtual entities into line/polygon buckets
                        try:
                            for se in e.virtual_entities():
                                sl = getattr(se.dxf, "layer", "0") or "0"
                                if sel_layers and sl not in sel_layers:
                                    continue
                                st = se.dxftype()
                                if st in {"LINE","LWPOLYLINE","POLYLINE","ARC","CIRCLE","ELLIPSE","SPLINE"}:
                                    try:
                                        pts = _flatten_with_path(se, flat_dist_precise)
                                    except Exception:
                                        if st == "LINE":
                                            s, ed = se.dxf.start, se.dxf.end
                                            pts = [(s.x, s.y, 0.0), (ed.x, ed.y, 0.0)]
                                        elif st in {"LWPOLYLINE","POLYLINE"}:
                                            pts = _fallback_polyline(se, include_3d=False)
                                        elif st == "CIRCLE":
                                            pts = _fallback_circle(se, flat_dist_precise)
                                        elif st == "ARC":
                                            pts = _fallback_arc(se, flat_dist_precise)
                                        else:
                                            pts = []
                                    _push_line_from_pts(pts)
                                elif st in {"HATCH","3DFACE","THREE_D_FACE","SOLID"}:
                                    for ring in _flatten_hatch_rings(se):
                                        gtype, geom = _coords_to_geom(ring)
                                        if gtype == "POLYGON" and geom:
                                            polys.append(geom)
                        except Exception as ex:
                            say(f"[warn] virtual_entities failed: {ex}")

                        # Decide merge strategy based on size/time
                        elapsed_ms = (time.perf_counter() - t0) * 1000.0
                        strategy = "robust"
                        if segs_seen > keep_merge_medium_limit:
                            strategy = "explode"
                        elif segs_seen > keep_merge_small_limit or elapsed_ms > keep_merge_time_soft_ms:
                            strategy = "graph"

                        merged = None
                        gtype = None

                        # Prefer polygons if found
                        if polys:
                            try:
                                merged = unary_union(polys)
                                if merged and not getattr(merged, "is_empty", False):
                                    gtype = "POLYGON"
                            except Exception as ex:
                                say(f"[warn] polygon union failed: {ex}")

                        # Otherwise try to merge lines
                        if gtype is None and lines:
                            if strategy == "robust":
                                merged = _merge_lines_robust(lines, line_merge_tol, say)
                                if merged is None or getattr(merged, "is_empty", False):
                                    strategy = "graph"
                            if gtype is None and strategy == "graph":
                                merged = _merge_lines_graph(lines, line_merge_tol)
                                merged = _extract_lineal(merged)
                                if merged is None or getattr(merged, "is_empty", False):
                                    strategy = "explode"
                            if gtype is None and strategy == "explode":
                                # Emit each line individually (still include block meta)
                                n = 0
                                for ls in lines:
                                    try:
                                        if ls and not getattr(ls, "is_empty", False):
                                            row = {"layer": layer, "geom": "LINE", "geometry": ls}
                                            row.update(meta)
                                            rows.append(row)
                                            n += 1
                                    except Exception:
                                        continue
                                say(f"[keep] block={meta.get('block_name','')} explode lines: {n}")
                                continue
                            if merged is not None:
                                gtype = "LINE"

                        # Emit merged output (LINE or POLYGON), or block insert point
                        if merged is not None and gtype is not None:
                            row = {"layer": layer, "geom": gtype, "geometry": merged}
                            row.update(meta)
                            rows.append(row)
                        else:
                            ip = e.dxf.insert
                            row = {"layer": layer, "geom": "POINT", "geometry": Point(ip.x, ip.y)}
                            row.update(meta)
                            rows.append(row)
                        continue

                    # explode mode: emit each virtual entity directly (with block meta)
                    expanded = False
                    try:
                        for se in e.virtual_entities():
                            sl = getattr(se.dxf, "layer", "0") or "0"
                            if sel_layers and sl not in sel_layers:
                                continue
                            rws = _precise_rows_from_entity(se, sl, include_3d, flat_dist_precise)
                            for r in rws:
                                r.update(meta)
                            rows += rws
                            expanded = True
                    except Exception as ex:
                        say(f"[warn] INSERT explode failed: {ex}")
                    if not expanded:
                        ip = e.dxf.insert
                        row = {"layer": layer, "geom": "POINT", "geometry": Point(ip.x, ip.y)}
                        row.update(meta)
                        rows.append(row)
                    continue

                # Non-INSERT entities
                rows += _precise_rows_from_entity(e, layer, include_3d, flat_dist_precise)
            except Exception as ex:
                say(f"[warn] entity failed: {ex}")
                continue

        say(f"[convert] done file: {path}, total rows {len(rows)}")

    if not rows:
        say("[convert] no rows")
        return {}

    import geopandas as gpd
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{source_epsg or 4326}")

    # Optional bbox filter (interpreted in WGS84)
    if bbox_wgs84 and isinstance(bbox_wgs84, (list, tuple)) and len(bbox_wgs84) == 4:
        from shapely.geometry import box
        say(f"[convert] bbox filter: {bbox_wgs84}")
        gdf4326 = gdf.to_crs(4326)
        mask = gdf4326.intersects(box(*bbox_wgs84))
        gdf = gdf.loc[mask].copy()
        say(f"[convert] inside bbox: {len(gdf)}")

    # Reproject to target EPSG if provided
    if target_epsg and int(target_epsg) != 4326:
        say(f"[convert] reproject to EPSG:{int(target_epsg)}")
        gdf = gdf.to_crs(epsg=int(target_epsg))

    # Add X/Y/Z columns for POINT features (in final CRS)
    try:
        def _pt_coords(g):
            try:
                if getattr(g, "geom_type", "") == "Point":
                    coords = list(g.coords)[0]
                elif getattr(g, "geom_type", "") == "MultiPoint" and len(getattr(g, "geoms", [])) > 0:
                    coords = list(g.geoms[0].coords)[0]
                else:
                    # Not a point-like geometry: use centroid
                    c = g.centroid
                    coords = list(c.coords)[0]
                x = float(coords[0]); y = float(coords[1])
                z = float(coords[2]) if len(coords) >= 3 else None
                return x, y, z
            except Exception:
                return None, None, None

        is_point = gdf["geom"].str.upper().eq("POINT")
        if is_point.any():
            xs, ys, zs = [], [], []
            for g in gdf.loc[is_point, "geometry"]:
                x, y, z = _pt_coords(g)
                xs.append(x); ys.append(y); zs.append(z)
            gdf.loc[is_point, "X"] = xs
            gdf.loc[is_point, "Y"] = ys
            gdf.loc[is_point, "Z"] = zs
            say(f"[convert] POINT attributes added: X/Y/Z for {is_point.sum()} features")
    except Exception as ex:
        say(f"[convert] add XY(Z) failed: {ex}")

    # Group into buckets by (layer, geom)
    out: Dict[Tuple[str, str], "gpd.GeoDataFrame"] = {}
    for (layer, geom), sub in gdf.groupby(["layer", "geom"]):
        say(f"[group] {layer} / {geom}: {len(sub)}")
        out[(str(layer), str(geom))] = sub.reset_index(drop=True)
    say(f"[convert] grouped buckets: {len(out)}")
    return out


# -------------------------
# Public UI wrapper (v1.2 signature)
# -------------------------
def precise_convert(
    src_path: str = None,
    target_layers: List[str] = None,
    layers: List[str] = None,
    output_path: str = None,
    out_path: str = None,
    output_dir: str = None,
    driver: str = None,
    overwrite: bool = True,
    source_epsg: int = 3826,
    src_epsg: int = None,
    target_epsg: int = None,
    tgt_epsg: int = None,
    merge_tolerance: float = 0.2,   # maps to line_merge_tol
    block_mode: str = "keep-merge",
    include_3d: bool = False,
    bbox_wgs84: Optional[Tuple[float, float, float, float]] = None,
    flat_dist_precise: float = 0.2,
    on_progress: ProgressCB = None,
    **kwargs
):
    """
    Thin wrapper compatible with the 1.2 UI layer, internally using the
    v1.1 ezdxf pipeline. Returns a dict[(layer, geom)] → GeoDataFrame.
    """
    dxf_paths = _coerce_paths(src_path, kwargs.get("dxf_paths"))
    if not dxf_paths:
        raise TypeError("precise_convert: missing 'src_path' or 'dxf_paths'.")

    lyr_list = target_layers if target_layers is not None else layers
    if lyr_list is not None and not isinstance(lyr_list, (list, tuple)):
        lyr_list = [str(lyr_list)]

    s_epsg = src_epsg if src_epsg is not None else source_epsg
    t_epsg = tgt_epsg if tgt_epsg is not None else target_epsg

    return _precise_convert_ezdxf(
        dxf_paths=dxf_paths,
        source_epsg=int(s_epsg) if s_epsg else 3826,
        target_epsg=int(t_epsg) if t_epsg else None,
        include_3d=include_3d,
        flat_dist_precise=float(flat_dist_precise or 0.2),
        target_layers=list(lyr_list) if lyr_list else None,
        bbox_wgs84=bbox_wgs84,
        on_progress=on_progress,
        block_mode=block_mode or "keep-merge",
        line_merge_tol=float(merge_tolerance or 0.2),
    )


# -------------------------
# Writers (GPKG / SHP / Fallbacks)
# -------------------------
def write_outputs(
    buckets: Dict[Tuple[str, str], "gpd.GeoDataFrame"],
    out_path: str,
    driver: str,
    overwrite: bool = False,
    on_progress: ProgressCB = None,
):
    """
    Write grouped GeoDataFrames to:
      - GPKG (preferred): one GPKG file, multiple layers by CAD layer name
      - ESRI Shapefile (Fiona): one .shp per (layer, geom) as <Layer>_<Geom>.shp
      - Fallbacks: pyshp (attempt to keep key fields) or GeoJSON
    Returns a list of dicts {path, layer, count}.
    """
    written = []
    import os
    from pathlib import Path

    def say(m):
        if on_progress:
            try: on_progress(m)
            except Exception:
                pass

    if not buckets:
        say("[write] empty buckets")
        return written

    # ----- GPKG -----
    if (driver and driver.upper() == "GPKG") or (out_path and out_path.lower().endswith(".gpkg")):
        gpkg = out_path if out_path.lower().endswith(".gpkg") else (out_path.rstrip("\\/") + "\\bundle.gpkg")
        try:
            import geopandas as gpd  # noqa
        except Exception as ex:
            say(f"[write:error] GeoPandas/Fiona required for GPKG: {ex}")
            return written
        if overwrite and os.path.exists(gpkg):
            try: os.remove(gpkg)
            except: pass
        for (layer, geom), gdf in buckets.items():
            try:
                lname = str(layer)
                gdf = _normalize_bucket_geoms((layer, geom), gdf)
                if gdf.empty:
                    say(f"[write:skip] {layer}/{geom} empty after normalize"); continue
                gdf.to_file(gpkg, layer=lname, driver="GPKG")
                written.append({"path": gpkg, "layer": lname, "count": int(len(gdf))})
                say(f"[write] GPKG: {lname} ({len(gdf)}) → {gpkg}")
            except Exception as ex:
                say(f"[write:error] GPKG layer {layer} failed: {ex}")
        return written

    # ----- ESRI Shapefile via Fiona -----
    os.makedirs(out_path, exist_ok=True)
    ok_any = False
    errors = []
    for (layer, geom), gdf in buckets.items():
        safe_layer = _sanitize_filename(layer)
        safe_geom = _sanitize_filename(geom)
        fpath = str(Path(out_path) / f"{safe_layer}_{safe_geom}.shp")
        if overwrite:
            for ext in (".shp",".shx",".dbf",".cpg",".prj",".qpj"):
                try: os.remove(os.path.splitext(fpath)[0]+ext)
                except: pass
        try:
            gdf = _normalize_bucket_geoms((layer, geom), gdf)
            if gdf.empty:
                say(f"[write:skip] {layer}/{geom} empty after normalize"); continue
            gdf.to_file(fpath, driver="ESRI Shapefile")
            written.append({"path": fpath, "layer": layer, "count": int(len(gdf))})
            say(f"[write] SHP: {layer} ({len(gdf)}) → {fpath}")
            ok_any = True
        except Exception as ex:
            errors.append((fpath, ex))
            say(f"[write:warn] Fiona failed, will try pyshp/GeoJSON: {fpath} → {ex}")

    if ok_any:
        return written

    # ----- pyshp fallback (keeps key fields; subject to 10-char field name limit) -----
    try:
        import shapefile as pyshp
        from shapely.geometry import mapping as shp_mapping
        say("[write] using pyshp fallback")

        def _short(name: str) -> str:
            """Truncate/clean field names to <=10 chars for Shapefile."""
            s = _sanitize_filename(name).upper()
            return s[:10] if len(s) > 10 else s

        def _shapeType(geom_type: str):
            gt = (geom_type or "").upper()
            return pyshp.POINT if gt=="POINT" else pyshp.POLYLINE if gt=="LINE" else pyshp.POLYGON if gt=="POLYGON" else pyshp.NULL

        for (layer, geom), gdf in buckets.items():
            gdf = _normalize_bucket_geoms((layer, geom), gdf)
            if gdf.empty:
                say(f"[write:skip] {layer}/{geom} empty after normalize"); continue

            shp_type = _shapeType(geom)
            if shp_type == pyshp.NULL:
                say(f"[write:warn] unsupported geom: {geom}"); continue

            safe_layer = _sanitize_filename(layer)
            safe_geom = _sanitize_filename(geom)
            fpath = str(Path(out_path) / f"{safe_layer}_{safe_geom}_pyshp.shp")

            # Columns (geometry column excluded)
            cols = list(gdf.columns)
            if "geometry" in cols:
                cols.remove("geometry")

            # Prefer important fields first
            preferred = ["block_name","BLK_X","BLK_Y","BLK_Z","BLK_ROT","BLK_SX","BLK_SY","BLK_SZ","X","Y","Z"]
            ordered = preferred + [c for c in cols if c not in preferred]

            # Define fields
            w = pyshp.Writer(fpath, shp_type)
            w.field(_short("FID"), "N", 18, 0)
            for c in ordered:
                try:
                    if c in gdf.select_dtypes(include=["int","float"]).columns:
                        w.field(_short(c), "F", 19, 6)
                    else:
                        w.field(_short(c), "C", 254)
                except Exception:
                    # If field definition fails, skip it silently
                    pass

            # Write geometries + attributes
            for i, row in gdf.reset_index(drop=True).iterrows():
                g = shp_mapping(row.geometry)
                if shp_type == pyshp.POINT:
                    x, y = g["coordinates"][:2]; w.point(x, y)
                elif shp_type == pyshp.POLYLINE:
                    coords = list(g["coordinates"])
                    parts = [list(ls) for ls in coords] if g["type"] == "MultiLineString" else [coords]
                    w.line(parts)
                elif shp_type == pyshp.POLYGON:
                    if g["type"] == "MultiPolygon":
                        parts = [list(ring) for poly in g["coordinates"] for ring in poly]
                    else:
                        parts = [list(r) for r in g["coordinates"]]
                    w.poly(parts)

                rec = [int(i)]
                for c in ordered:
                    try:
                        v = row.get(c, None)
                        if v is None:
                            rec.append("")
                        elif isinstance(v, (int, float)):
                            rec.append(v)
                        else:
                            s = str(v)
                            rec.append(s if len(s) <= 254 else s[:254])
                    except Exception:
                        rec.append("")
                # Ensure record length matches field count (excluding the deletion flag)
                while len(rec) < len(w.fields) - 1:
                    rec.append("")
                w.record(*rec)
            w.close()

            written.append({"path": fpath, "layer": layer, "count": int(len(gdf))})
            say(f"[write] pyshp: {layer} ({len(gdf)}) → {fpath}")

        if written:
            return written
    except Exception as ex:
        say(f"[write:warn] pyshp unavailable or failed: {ex}, fallback to GeoJSON")

    # ----- GeoJSON fallback -----
    say("[write] using GeoJSON fallback")
    for (layer, geom), gdf in buckets.items():
        gdf = _normalize_bucket_geoms((layer, geom), gdf)
        if gdf.empty:
            say(f"[write:skip] {layer}/{geom} empty after normalize"); continue
        safe_layer = _sanitize_filename(layer)
        safe_geom = _sanitize_filename(geom)
        fpath = str(Path(out_path) / f"{safe_layer}_{safe_geom}.geojson")
        try:
            feats = []
            for i, row in gdf.reset_index(drop=True).iterrows():
                from shapely.geometry import mapping as shp_mapping2
                feat = {
                    "type":"Feature",
                    "properties":{},
                    "geometry": shp_mapping2(row.geometry),
                }
                for k, v in row.items():
                    if k == "geometry":
                        continue
                    feat["properties"][k] = None if v is None else (float(v) if isinstance(v,(int,float)) else str(v))
                feats.append(feat)
            with open(fpath, "w", encoding="utf-8") as f:
                import json; json.dump({"type":"FeatureCollection","features":feats}, f, ensure_ascii=False)
            written.append({"path": fpath, "layer": layer, "count": int(len(feats))})
            say(f"[write] GeoJSON: {layer} ({len(feats)}) → {fpath}")
        except Exception as ex:
            say(f"[write:error] GeoJSON failed: {fpath} → {ex}")
    return written
