# CAD to GIS Converter (QGIS Plugin)

**Convert DXF/DWG to GIS layers — fast, reliable, and Processing-free.**  
This plugin adds a menu entry in QGIS that opens a clean dialog for CAD→GIS conversion with a live **Layer Preview**, DWG→DXF fallback, and safe dependency setup.

> **QGIS:** 3.22+ (tested on 3.28 LTR)  
> **Python:** 3.9+ (uses QGIS-bundled Python on Windows)

---

## ✨ Features

- **DXF & DWG support**
  - DWG is auto-converted to a temporary DXF (via your local ODA/LibreDWG tools) before parsing.
- **Layer Preview**
  - Scan CAD layers before running; multi-select or check and one-click copy to the CSV field.
- **Block handling**
  - `keep-merge` (preserve blocks, merge lines) or `explode`.
- **Zero-default merge**
  - *Line-merge tolerance* default is **0.0** (strict merging unless you opt in).
- **Flexible outputs**
  - Write to **GeoPackage (GPKG)** or **ESRI Shapefile**; optionally auto-load into the project.
- **Safe dependency installer**
  - Automatically installs `ezdxf` using the QGIS-bundled `python.exe` (no extra QGIS window).

---

## 🚀 Quick Start

1. **Plugins → CAD to GIS Converter**.
2. **Input CAD**: choose a `.dxf` or `.dwg`.
3. Click **Scan Layers** to populate the **Layer Preview** list.  
   Select or check layers → **Copy selected from preview** (leave empty to include all).
4. Set **Source EPSG** and optional **Target EPSG**.
5. Choose **Block handling** (`keep-merge` / `explode`) and **Line-merge tolerance** (default `0.0`).
6. Choose **Output driver** (GPKG/SHP) and an **Output path**.
7. (Optional) **Overwrite existing** / **Load outputs into project**.
8. Click **Run**. Progress and a clickable summary appear in the log pane.

---

## 🧰 Parameters

| Option | Description |
|---|---|
| **Input CAD (DXF/DWG)** | Source CAD file. DWG is auto-converted to a temporary DXF. |
| **Layer names (CSV)** | Filter by layer names (comma-separated). Leave empty for all. |
| **Source EPSG** | EPSG code of the input CAD CRS (e.g., `3826`). |
| **Target EPSG (optional)** | Reproject outputs; leave blank to keep source CRS. |
| **Block handling** | `keep-merge` (preserve blocks, merge lines) or `explode`. |
| **Line-merge tolerance** | Merge tolerance in layer units. **Default: `0.0`**. |
| **Output driver** | `GPKG` or `ESRI Shapefile`. |
| **Output path** | GPKG: a `.gpkg` file; SHP: a folder to contain `.shp` layers. |
| **DWG converter preference** | Hint for DWG→DXF backend: `auto` / `oda` / `libredwg`. |
| **DXF version for DWG conversion** | e.g., `ACAD2013` (default). |
| **Overwrite existing** | Replace existing outputs. |
| **Load outputs into project** | Add results to the current QGIS project. |

## 🔍 How It Works

- **DXF** is read directly with `ezdxf`.
- **DWG** is converted to a temporary DXF via `dwg_support.py`, then processed as DXF.
- `conversion_service.py` handles parsing, block handling, optional merging, reprojection, and writing outputs.
- The plugin **does not** use QGIS Processing; it calls your services directly to avoid `createInstance()` issues.

---

## 🤝 Contributing

Issues and PRs are welcome.  
It is made by Mingyi Hsu

---


## 🙌 Acknowledgements

- [`ezdxf`](https://github.com/mozman/ezdxf)
- ODA / LibreDWG tools
- QGIS community
