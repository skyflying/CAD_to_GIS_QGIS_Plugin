# -*- coding: utf-8 -*-

import os, shutil, traceback, html
from qgis.PyQt.QtWidgets import (QDialog, QFileDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QWidget, QTextBrowser, QGridLayout, QListWidget, QListWidgetItem,
    QAbstractItemView, QMessageBox, QProgressDialog)
from qgis.PyQt.QtGui import QTextCursor
from qgis.PyQt.QtCore import Qt, QCoreApplication
from qgis.core import QgsVectorLayer, QgsProject
from .services import dwg_support as dwg
from .services import deps as _deps
from .services import augment as _augment
import logging

_log = logging.getLogger(__name__)

# OGR fallback imports
# NOTE: do NOT call ogr.UseExceptions() or gdal.SetConfigOption() at import time:
# both are process-wide and silently changed how QGIS itself (and other plugins)
# open DXF files as soon as this plugin was loaded.
try:
    from osgeo import ogr, gdal, osr
except Exception:
    ogr = None
    gdal = None
    osr = None

def _ogr_list_layers(dxf_path: str):
    names = []
    if ogr is None or not os.path.exists(dxf_path):
        return names
    # same encoding handling as the conversion engine (R2007+ DXF is UTF-8)
    from .services.conversion_service import _dxf_encoding_hint, _gdal_config
    enc = _dxf_encoding_hint(dxf_path)
    with _gdal_config({"DXF_ENCODING": enc} if enc else {}):
        ds = ogr.Open(dxf_path, 0)
    if ds is None:
        return names
    lyr = ds.GetLayerByName("entities") or (ds.GetLayer(0) if ds.GetLayerCount() else None)
    if lyr is None:
        ds = None; return names
    try:
        res = ds.ExecuteSQL("SELECT DISTINCT Layer FROM entities")
        if res:
            for f in res:
                v = f.GetField("Layer")
                if v:
                    names.append(str(v))
            ds.ReleaseResultSet(res)
    except Exception:
        _log.debug("_ogr_list_layers: ignored exception", exc_info=True)
    if not names:
        seen = set()
        lyr.ResetReading()
        for f in lyr:
            try:
                v = f.GetField("Layer")
                if v and v not in seen:
                    seen.add(v); names.append(str(v))
            except Exception:
                _log.debug("_ogr_list_layers: ignored exception", exc_info=True)
    ds = None
    return sorted(set(names))

class CadToGisDialog(QDialog):
    def _parse_epsg(self, widget, default=None):
        # 相容 QSpinBox / QLineEdit
        try:
            return int(widget.value())  # QSpinBox
        except Exception:
            _log.debug("_parse_epsg: ignored exception", exc_info=True)
        try:
            txt = widget.text().strip()
            return int(txt) if txt else default
        except Exception:
            return default
        
    def __init__(self, parent_or_iface):
        # Accept either QgisInterface or a QWidget (e.g., QMainWindow)
        try:
            from qgis.utils import iface as _global_iface
        except Exception:
            _global_iface = None
        parent = None
        self.iface = None
        # If we got a QgisInterface-like object
        if hasattr(parent_or_iface, 'mainWindow') and callable(getattr(parent_or_iface, 'mainWindow')):
            self.iface = parent_or_iface
            try:
                parent = parent_or_iface.mainWindow()
            except Exception:
                parent = None
        else:
            # Assume it's already a QWidget parent (e.g., QMainWindow)
            parent = parent_or_iface
            self.iface = _global_iface
        super().__init__(parent)

        self.setWindowTitle("CAD to GIS Converter")
        self.setMinimumWidth(860)

        grid = QGridLayout()

        # Row 0: Input + buttons
        grid.addWidget(QLabel("Input CAD (DXF/DWG)"), 0, 0)
        self.in_edit = QLineEdit()
        btn_in = QPushButton("Browse"); btn_in.clicked.connect(self.browse_input)
        btn_scan = QPushButton("Scan Layers"); btn_scan.clicked.connect(self.scan_layers)
        wrap = QWidget(); hb = QHBoxLayout(wrap); hb.setContentsMargins(0,0,0,0)
        hb.addWidget(self.in_edit); hb.addWidget(btn_in); hb.addWidget(btn_scan)
        grid.addWidget(wrap, 0, 1, 1, 2)

        # Row 1: Layers CSV + control buttons
        grid.addWidget(QLabel("Layer names (CSV, auto-filled from preview)"), 1, 0)
        self.layers_edit = QLineEdit()
        btn_select_all = QPushButton("Select All"); btn_select_all.clicked.connect(self.select_all_layers)
        btn_clear = QPushButton("Clear"); btn_clear.clicked.connect(self.clear_layers_selection)
        wrap2 = QWidget(); hb2 = QHBoxLayout(wrap2); hb2.setContentsMargins(0,0,0,0)
        hb2.addWidget(self.layers_edit); hb2.addWidget(btn_select_all); hb2.addWidget(btn_clear)
        grid.addWidget(wrap2, 1, 1, 1, 2)

        # Row 2: Layer preview list
        grid.addWidget(QLabel("Layer Preview"), 2, 0, Qt.AlignmentFlag.AlignTop)
        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)  # Qt5/Qt6-safe
        self.layer_list.itemSelectionChanged.connect(self.sync_layers_csv_from_preview)
        self.layer_list.itemChanged.connect(self.sync_layers_csv_from_preview)
        grid.addWidget(self.layer_list, 2, 1, 1, 2)

        # Row 3: Source EPSG
        grid.addWidget(QLabel("Source EPSG"), 3, 0)
        self.src_epsg = QLineEdit("3826")   # empty used to fall back silently
        grid.addWidget(self.src_epsg, 3, 1, 1, 2)

        # Row 4: Target EPSG
        grid.addWidget(QLabel("Target EPSG (optional)"), 4, 0)
        self.tgt_epsg = QLineEdit()
        grid.addWidget(self.tgt_epsg, 4, 1, 1, 2)

        # Row 5: Block mode
        grid.addWidget(QLabel("Block handling"), 5, 0)
        self.block_mode = QComboBox(); self.block_mode.addItems(["keep-merge", "explode"])
        grid.addWidget(self.block_mode, 5, 1, 1, 2)

        # Row 6: Merge tolerance (default 0.0)
        grid.addWidget(QLabel("Line-merge tolerance"), 6, 0)
        self.merge_tol = QDoubleSpinBox(); self.merge_tol.setDecimals(6); self.merge_tol.setRange(0.0, 1e9); self.merge_tol.setValue(0.0)
        grid.addWidget(self.merge_tol, 6, 1, 1, 2)

        # Row 7: Spline tolerance
        grid.addWidget(QLabel("Spline tolerance"), 7, 0)
        self.spline_tol = QDoubleSpinBox(); self.spline_tol.setDecimals(6); self.spline_tol.setRange(0.0, 1e6); self.spline_tol.setValue(0.2)
        grid.addWidget(self.spline_tol, 7, 1, 1, 2)

        # Row 8: Driver
        grid.addWidget(QLabel("Output driver"), 8, 0)
        self.driver = QComboBox(); self.driver.addItems(["GPKG","ESRI Shapefile"])
        grid.addWidget(self.driver, 8, 1, 1, 2)

        # Row 9: Output path
        self.out_label = QLabel("Output GeoPackage (GPKG)")
        grid.addWidget(self.out_label, 9, 0)
        self.out_edit = QLineEdit()
        self.btn_out = QPushButton("Browse"); self.btn_out.clicked.connect(self.browse_output)
        wrap3 = QWidget(); hb3 = QHBoxLayout(wrap3); hb3.setContentsMargins(0,0,0,0)
        hb3.addWidget(self.out_edit); hb3.addWidget(self.btn_out)
        grid.addWidget(wrap3, 9, 1, 1, 2)

        # Row 10: DWG options
        grid.addWidget(QLabel("DWG converter preference"), 10, 0, Qt.AlignmentFlag.AlignTop)
        self.dwg_pref = QComboBox(); self.dwg_pref.addItems(["auto","oda","libredwg"])
        self.dwg_pref.setToolTip("auto: ODA → LibreDWG → GDAL CAD driver (DWG R2000 only)")
        # DWG reader status + tools under the combo
        self.lbl_dwg = QLabel()
        self.btn_set_oda = QPushButton("Set ODA path…"); self.btn_set_oda.clicked.connect(self.on_set_oda)
        self.btn_get_libre = QPushButton("Download LibreDWG"); self.btn_get_libre.clicked.connect(self.on_download_libredwg)
        self.btn_oda_page = QPushButton("ODA download page")
        self.btn_oda_page.clicked.connect(lambda: dwg.open_url(dwg.ODA_DOWNLOAD_URL))
        self.btn_redetect = QPushButton("Re-detect"); self.btn_redetect.clicked.connect(self.refresh_dwg_status)
        dwg_wrap = QWidget(); dvb = QVBoxLayout(dwg_wrap); dvb.setContentsMargins(0,0,0,0)
        dvb.addWidget(self.dwg_pref)
        dh = QHBoxLayout(); dh.addWidget(self.lbl_dwg, 1)
        for b in (self.btn_set_oda, self.btn_get_libre, self.btn_oda_page, self.btn_redetect):
            dh.addWidget(b)
        dvb.addLayout(dh)
        grid.addWidget(dwg_wrap, 10, 1, 1, 2)

        grid.addWidget(QLabel("DXF version for DWG conversion"), 11, 0)
        self.dxf_version = QLineEdit(); self.dxf_version.setText("ACAD2013")
        grid.addWidget(self.dxf_version, 11, 1, 1, 2)

        # Row 12: Options
        grid.addWidget(QLabel("Options"), 12, 0, Qt.AlignmentFlag.AlignTop)
        options_wrap = QWidget(); fl = QVBoxLayout(options_wrap); fl.setContentsMargins(0,0,0,0)
        self.chk_overwrite = QCheckBox("Overwrite existing"); self.chk_overwrite.setChecked(False)
        self.chk_load = QCheckBox("Load outputs into project"); self.chk_load.setChecked(True)
        self.chk_text_attrs = QCheckBox("Extract TEXT/MTEXT to attributes (annotation points) — only selected layers"); self.chk_text_attrs.setChecked(True)
        self.chk_block_attrs = QCheckBox("Expand block attributes into fields (BLOCKS layer) — only selected layers"); self.chk_block_attrs.setChecked(True)
        self.chk_block_transform = QCheckBox("Keep block transform fields (x,y,rotation,scale) — only selected layers"); self.chk_block_transform.setChecked(True)
        fl.addWidget(self.chk_overwrite); fl.addWidget(self.chk_load)
        fl.addWidget(self.chk_text_attrs); fl.addWidget(self.chk_block_attrs); fl.addWidget(self.chk_block_transform)
        # Python package status (geopandas etc. are not shipped with QGIS)
        dep_row = QHBoxLayout()
        self.lbl_deps = QLabel()
        self.btn_deps = QPushButton("Install Python packages…"); self.btn_deps.clicked.connect(self.on_install_deps)
        dep_row.addWidget(self.lbl_deps, 1); dep_row.addWidget(self.btn_deps)
        fl.addLayout(dep_row)
        grid.addWidget(options_wrap, 12, 1, 1, 2)

        # Row 13: Run
        run_bar = QWidget(); hb4 = QHBoxLayout(run_bar); hb4.setContentsMargins(0,0,0,0)
        self.btn_run = QPushButton("Run"); self.btn_run.clicked.connect(self.run_convert)
        hb4.addStretch(1); hb4.addWidget(self.btn_run)
        grid.addWidget(run_bar, 13, 0, 1, 3)

        # Row 14: Log/HTML
        self.out_html = QTextBrowser(); self.out_html.setOpenExternalLinks(True)
        grid.addWidget(self.out_html, 14, 0, 1, 3)

        self.driver.currentIndexChanged.connect(self.on_driver_changed)
        self.on_driver_changed()
        self.refresh_dwg_status()
        self.refresh_deps_status()

        lay = QVBoxLayout(self); lay.addLayout(grid)

    # ---------------- Python dependencies ----------------
    def refresh_deps_status(self):
        st = _deps.check_dependencies()
        self.lbl_deps.setText(f"Python: {st['summary']}")
        self.btn_deps.setVisible(bool(st["missing_required"] or st["missing_optional"]))
        return st

    def on_install_deps(self):
        st = _deps.check_dependencies()
        pkgs = list(st["missing_required"]) + list(st["missing_optional"])
        if not pkgs:
            self.refresh_deps_status(); return True
        text = _deps.install_instructions(pkgs)
        if not _deps.pip_install_supported():
            QMessageBox.information(self, "Python packages", text)
            return False
        ok = QMessageBox.question(
            self, "Install Python packages",
            text + "\nInstall now with pip into your user profile (internet required)?")
        if ok != QMessageBox.StandardButton.Yes:
            return False
        self.btn_run.setEnabled(False); self.btn_deps.setEnabled(False)
        self.log(f"<b>Installing:</b> {html.escape(' '.join(pkgs))}")
        try:
            rc = _deps.pip_install(pkgs, on_output=self._plain_log)
        except Exception as ex:
            rc = -1
            self.log(f"<div style='color:#b00'>pip failed: {html.escape(str(ex))}</div>")
        finally:
            self.btn_run.setEnabled(True); self.btn_deps.setEnabled(True)
        st = self.refresh_deps_status()
        if rc == 0 and st["ok"]:
            self.log("<b>Packages installed.</b> If conversion still fails, restart QGIS.")
            return True
        self.log("<span style='color:#b00'>Installation incomplete — see messages above, "
                 "or follow the manual steps (OSGeo4W Shell), then restart QGIS.</span>")
        return False

    # ---------------- DWG tools ----------------
    def refresh_dwg_status(self):
        st = dwg.detect_status()
        self.lbl_dwg.setText(f"DWG reader: {st['summary']}")
        self.lbl_dwg.setToolTip("\n".join([
            f"ODA: {st['oda'] or '-'}",
            f"LibreDWG: {st['libredwg'] or '-'}",
            f"GDAL CAD driver (DWG R2000 only): {'yes' if st['gdal_cad'] else 'no'}",
        ]))
        self.btn_get_libre.setEnabled(dwg.libredwg_download_supported())
        return st

    def on_set_oda(self):
        filt = "ODAFileConverter (ODAFileConverter*.exe)" if os.name == "nt" else "ODAFileConverter (ODAFileConverter*)"
        p, _ = QFileDialog.getOpenFileName(self, "Locate ODAFileConverter", "", filt)
        if p:
            dwg.set_oda_path(p)
            self.refresh_dwg_status()
            self.log(f"ODA path set: {html.escape(p)}")
        return bool(p)

    def on_download_libredwg(self):
        """Download LibreDWG (Windows) after confirmation; returns True on success."""
        if not dwg.libredwg_download_supported():
            QMessageBox.information(self, "LibreDWG",
                                    "Automatic download is Windows-only. Install LibreDWG (dwg2dxf) "
                                    "with your package manager, then press Re-detect.")
            return False
        ok = QMessageBox.question(
            self, "Download LibreDWG",
            "Download LibreDWG (GPL-3.0, ~12 MB) from github.com/LibreDWG/libredwg/releases into:\n"
            f"{dwg.config_dir()}\n\nContinue?")
        if ok != QMessageBox.StandardButton.Yes:
            return False
        dlg = QProgressDialog("Downloading LibreDWG…", None, 0, 100, self)
        dlg.setWindowTitle("LibreDWG"); dlg.setMinimumDuration(0); dlg.setValue(0)

        def pct(v):
            dlg.setValue(v); QCoreApplication.processEvents()
        try:
            exe = dwg.download_libredwg(on_progress=self._plain_log, on_percent=pct)
            self.log(f"<b>LibreDWG ready:</b> {html.escape(exe)}")
            return True
        except Exception as ex:
            self.log(f"<div style='color:#b00'><b>LibreDWG download failed:</b> {html.escape(str(ex))}</div>"
                     f"Behind a proxy/firewall: download libredwg-&lt;ver&gt;-win64.zip from "
                     f"<a href='{dwg.LIBREDWG_RELEASES_URL}'>{dwg.LIBREDWG_RELEASES_URL}</a>, unzip it into "
                     f"{html.escape(os.path.join(dwg.config_dir(), 'libredwg'))} and press Re-detect.")
            return False
        finally:
            dlg.close()
            self.refresh_dwg_status()

    def _resolve_cad_input(self, cad_path):
        """Return (path_for_engine, temp_dir_or_None) or (None, None) if the user cancelled.

        DXF -> unchanged.  DWG -> ODA/LibreDWG temp DXF, else GDAL CAD (R2000),
        else a guidance dialog (download LibreDWG / set ODA path / ODA page) and one retry.
        """
        if not cad_path.lower().endswith(".dwg"):
            return cad_path, None
        for attempt in (1, 2):
            try:
                kind, p = dwg.open_dwg(cad_path, prefer=self.dwg_pref.currentText(),
                                       dxf_version=(self.dxf_version.text().strip() or "ACAD2013"),
                                       on_progress=self._plain_log)
                if kind == "dxf":
                    return p, os.path.dirname(p)
                self.log("<span style='color:#b60'>No ODA / LibreDWG: reading DWG R2000 directly with GDAL. "
                         "Blocks are not merged; hatches, splines and some entities may be missing.</span>")
                return p, None
            except dwg.DwgUnavailableError as ex:
                if attempt == 2:
                    raise RuntimeError(ex.guidance)
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("Cannot read DWG")
                box.setText(ex.guidance)
                b_lib = box.addButton("Download LibreDWG", QMessageBox.ButtonRole.AcceptRole) if dwg.libredwg_download_supported() else None
                b_oda = box.addButton("Set ODA path…", QMessageBox.ButtonRole.ActionRole)
                b_page = box.addButton("ODA download page", QMessageBox.ButtonRole.ActionRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                clicked = box.clickedButton()
                if b_lib is not None and clicked is b_lib and self.on_download_libredwg():
                    continue
                if clicked is b_oda and self.on_set_oda():
                    continue
                if clicked is b_page:
                    dwg.open_url(dwg.ODA_DOWNLOAD_URL)
                self.log("<i>DWG reading cancelled.</i>")
                return None, None
        return None, None

    def browse_input(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select CAD file", "", "CAD (*.dxf *.dwg);;DXF (*.dxf);;DWG (*.dwg)")
        if p:
            self.in_edit.setText(p)
            self.scan_layers()

    def browse_output(self):
        if self.driver.currentText() == "GPKG":
            p, _ = QFileDialog.getSaveFileName(self, "Output GeoPackage", "", "GeoPackage (*.gpkg)")
        else:
            p = QFileDialog.getExistingDirectory(self, "Output folder for Shapefiles")
        if p: self.out_edit.setText(p)

    def on_driver_changed(self):
        if self.driver.currentText() == "GPKG":
            self.out_label.setText("Output GeoPackage (GPKG)")
            self.btn_out.setText("Browse")
        else:
            self.out_label.setText("Output folder (SHP)")
            self.btn_out.setText("Select Folder")

    def log(self, msg):
        if msg:
            self.out_html.append(msg.replace("\n","<br>"))
            try:
            # 滾到最底，確保新訊息可見
                self.out_html.moveCursor(QTextCursor.MoveOperation.End)
            except Exception:
                _log.debug("log: ignored exception", exc_info=True)
        # ★ 關鍵：立即處理事件，讓 UI 不必等任務結束才重畫
            QCoreApplication.processEvents()

    def _plain_log(self, s):
        """Progress text from the engine is plain text (may contain '<' in layer names)."""
        self.log(html.escape(str(s or "")))

    def sync_layers_csv_from_preview(self):
        names = set()
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            if it.checkState() == Qt.CheckState.Checked or it.isSelected():
                names.add(it.text())
        self.layers_edit.setText(", ".join(sorted(names)))

    def select_all_layers(self):
        self.layer_list.blockSignals(True)
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            it.setSelected(True)
            it.setCheckState(Qt.CheckState.Checked)
        self.layer_list.blockSignals(False)
        self.sync_layers_csv_from_preview()

    def clear_layers_selection(self):
        self.layer_list.blockSignals(True)
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            it.setSelected(False)
            it.setCheckState(Qt.CheckState.Unchecked)
        self.layer_list.blockSignals(False)
        self.sync_layers_csv_from_preview()

    def scan_layers(self):
        self.layer_list.clear()
        cad_path = self.in_edit.text().strip()
        if not cad_path or not os.path.isfile(cad_path):
            self.log("<span style='color:#b00'>Please pick a valid DXF/DWG first.</span>")
            return
        # Try to prepare ezdxf; do not abort if unavailable
        ez_ok = _deps.ensure_ezdxf_safe(feedback=SimpleFeedback(self))

        temp_dir = None
        try:
            src_for_scan, temp_dir = self._resolve_cad_input(cad_path)
            if src_for_scan is None:
                return

            names = []
            if src_for_scan.lower().endswith(".dwg"):      # GDAL CAD driver (R2000)
                names = dwg.gdal_cad_layers(src_for_scan)
            elif ez_ok:
                try:
                    import ezdxf
                    self.log("<b>Reading layers via ezdxf ...</b>")
                    doc = ezdxf.readfile(src_for_scan)
                    names = sorted([str(t.dxf.name) for t in doc.layers])
                except Exception as e:
                    self.log(f"<i>ezdxf layer scan failed:</i> {e}")
            if not names and not src_for_scan.lower().endswith(".dwg"):
                self.log("Trying OGR fallback ...")
                names = _ogr_list_layers(src_for_scan)

            if not names:
                self.log("<i>No layers found.</i>")
            else:
                for nm in names:
                    it = QListWidgetItem(nm); it.setCheckState(Qt.CheckState.Unchecked)
                    self.layer_list.addItem(it)
                self.log(f"Found {len(names)} layer(s).")
        except Exception as e:
            tb = traceback.format_exc()
            self.log(f"<div style='color:#b00'><b>Layer scan failed:</b> {e}</div>")
            self.log(f"<pre>{tb}</pre>")
        finally:
            if temp_dir and os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def run_convert(self):
        
 
        try:
            self.btn_run.setEnabled(False)
            dep = self.refresh_deps_status()
            if not dep["ok"] and not self.on_install_deps():
                self.log("<span style='color:#b00'>Missing Python packages: "
                         f"{html.escape(', '.join(dep['missing_required']))}. Conversion not started.</span>")
                return
            ez_ok = _deps.ensure_ezdxf_safe(feedback=SimpleFeedback(self))

            from .services.conversion_service import precise_convert, write_outputs

            cad_path = self.in_edit.text().strip()
            if not cad_path or not os.path.isfile(cad_path):
                raise RuntimeError(f"File not found: {cad_path}")

            layers_csv = self.layers_edit.text().strip()
            target_layers = [s.strip() for s in layers_csv.split(',') if s.strip()] if layers_csv else []
            src_epsg = self._parse_epsg(self.src_epsg)
            if not src_epsg:
                raise RuntimeError("Please enter the Source EPSG (e.g. 3826 for TWD97 / TM2 zone 121).")
            tgt_epsg = self._parse_epsg(self.tgt_epsg)
            mode = self.block_mode.currentText()
            merge_tol = float(self.merge_tol.value())
            spline_tol = float(self.spline_tol.value())
            driver = self.driver.currentText()
            out_path = self.out_edit.text().strip()
            if not out_path:
                raise RuntimeError("Please specify output path.")
            if driver == "GPKG" and not out_path.lower().endswith(".gpkg"):
                out_path += ".gpkg"
            dwg_pref = self.dwg_pref.currentText()
            dxf_version = self.dxf_version.text().strip() or "ACAD2013"
            overwrite = self.chk_overwrite.isChecked()
            do_load = self.chk_load.isChecked()

            input_for_convert, temp_dir = self._resolve_cad_input(cad_path)
            if input_for_convert is None:
                return

            try:
                self.log("<b>Running conversion ...</b>")
                buckets = precise_convert(
                    src_path=input_for_convert,                    # ← 傳字串，不要包 list
                    source_epsg=src_epsg,
                    target_epsg=tgt_epsg,
                    include_3d=False,
                    bbox_wgs84=None,
                    target_layers=(target_layers if target_layers else None),
                    block_mode=mode,
                    merge_tolerance=merge_tol,                     # ← 正確參數名
                    flat_dist_precise=spline_tol,
                    on_progress=self._plain_log
                )

                # Optional augment (needs ezdxf + a DXF; skipped for direct GDAL-CAD DWG reads)
                try:
                    if (_augment and not input_for_convert.lower().endswith(".dwg")
                            and (self.chk_text_attrs.isChecked() or self.chk_block_attrs.isChecked())):
                        extras = _augment.collect_annotations_and_blocks(
                            input_for_convert,
                            src_epsg=src_epsg,
                            tgt_epsg=tgt_epsg,
                            include_text=self.chk_text_attrs.isChecked(),
                            include_blocks=self.chk_block_attrs.isChecked(),
                            keep_block_transform=self.chk_block_transform.isChecked(),
                            target_layers=(target_layers if target_layers else None),
                            on_progress=self._plain_log,
                        )
                        if extras and isinstance(buckets, dict):
                            buckets.update(extras)
                except Exception as e_aug:
                    self.log(f"<i>augment skipped:</i> {e_aug}")

                self.log("<b>Writing outputs ...</b>")
                written = write_outputs(
                    buckets,
                    out_path=out_path,
                    driver="GPKG" if driver=="GPKG" else "ESRI Shapefile",
                    overwrite=overwrite,
                    on_progress=self._plain_log
                )
            finally:
                if temp_dir and os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)

            # Auto load
            if do_load:
                for w in (written or []):
                    path = w.get("path"); name = w.get("layer")
                    uri = path if driver == "ESRI Shapefile" else f"{path}|layername={name}"
                    v = QgsVectorLayer(uri, name, "ogr")
                    if v and v.isValid():
                        QgsProject.instance().addMapLayer(v)

            summary = "<h3>CAD to GIS Converter</h3><ul>" + "\n".join(
                [f"<li><b>{html.escape(str(w.get('layer')))}</b> ({w.get('count')}) → {html.escape(str(w.get('path')))}</li>"
                 for w in (written or [])]
            ) + "</ul>"
            self.out_html.append(summary)
            self.log("<b>Done.</b>")
        except Exception as e:
            tb = traceback.format_exc()
            self.out_html.append(f"<div style='color:#b00'><b>ERROR:</b> {e}</div><pre>{tb}</pre>")
        finally:
            self.btn_run.setEnabled(True)

class SimpleFeedback:
    def __init__(self, dlg: CadToGisDialog):
        self.dlg = dlg
    def pushInfo(self, s): self.dlg.log(s or "")
