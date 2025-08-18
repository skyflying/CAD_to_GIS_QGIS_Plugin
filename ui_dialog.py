import os, shutil, traceback
from qgis.PyQt.QtWidgets import (QDialog, QFileDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QWidget, QTextBrowser, QGridLayout, QListWidget, QListWidgetItem)
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsVectorLayer, QgsProject
from .services.dwg_support import dwg_to_temp_dxf_auto
from .services import deps as _deps

class CadToGisDialog(QDialog):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("CAD to GIS Converter")
        self.setMinimumWidth(760)

        grid = QGridLayout()

        # Row 0: Input + buttons
        grid.addWidget(QLabel("Input CAD (DXF/DWG)"), 0, 0)
        self.in_edit = QLineEdit()
        btn_in = QPushButton("Browse"); btn_in.clicked.connect(self.browse_input)
        btn_scan = QPushButton("Scan Layers"); btn_scan.clicked.connect(self.scan_layers)
        wrap = QWidget(); hb = QHBoxLayout(wrap); hb.setContentsMargins(0,0,0,0)
        hb.addWidget(self.in_edit); hb.addWidget(btn_in); hb.addWidget(btn_scan)
        grid.addWidget(wrap, 0, 1, 1, 2)

        # Row 1: Layers CSV + copy from preview
        grid.addWidget(QLabel("Layer names (CSV, optional)"), 1, 0)
        self.layers_edit = QLineEdit()
        btn_copy_selected = QPushButton("Copy selected from preview"); btn_copy_selected.clicked.connect(self.copy_selected_layers)
        wrap2 = QWidget(); hb2 = QHBoxLayout(wrap2); hb2.setContentsMargins(0,0,0,0)
        hb2.addWidget(self.layers_edit); hb2.addWidget(btn_copy_selected)
        grid.addWidget(wrap2, 1, 1, 1, 2)

        # Row 2: Layer preview list
        grid.addWidget(QLabel("Layer Preview"), 2, 0, Qt.AlignTop)
        self.layer_list = QListWidget(); self.layer_list.setSelectionMode(self.layer_list.MultiSelection)
        grid.addWidget(self.layer_list, 2, 1, 1, 2)

        # Row 3: Source EPSG
        grid.addWidget(QLabel("Source EPSG"), 3, 0)
        self.src_epsg = QSpinBox(); self.src_epsg.setMaximum(999999); self.src_epsg.setValue(3826)
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

        # Row 7: Driver
        grid.addWidget(QLabel("Output driver"), 7, 0)
        self.driver = QComboBox(); self.driver.addItems(["GPKG","ESRI Shapefile"])
        grid.addWidget(self.driver, 7, 1, 1, 2)

        # Row 8: Output path
        self.out_label = QLabel("Output GeoPackage (GPKG)")
        grid.addWidget(self.out_label, 8, 0)
        self.out_edit = QLineEdit()
        self.btn_out = QPushButton("Browse"); self.btn_out.clicked.connect(self.browse_output)
        wrap3 = QWidget(); hb3 = QHBoxLayout(wrap3); hb3.setContentsMargins(0,0,0,0)
        hb3.addWidget(self.out_edit); hb3.addWidget(self.btn_out)
        grid.addWidget(wrap3, 8, 1, 1, 2)

        # Row 9: DWG options
        grid.addWidget(QLabel("DWG converter preference"), 9, 0)
        self.dwg_pref = QComboBox(); self.dwg_pref.addItems(["auto","oda","libredwg"])
        grid.addWidget(self.dwg_pref, 9, 1, 1, 2)

        grid.addWidget(QLabel("DXF version for DWG conversion"), 10, 0)
        self.dxf_version = QLineEdit(); self.dxf_version.setText("ACAD2013")
        grid.addWidget(self.dxf_version, 10, 1, 1, 2)

        # Row 11: Flags
        self.chk_overwrite = QCheckBox("Overwrite existing"); self.chk_overwrite.setChecked(False)
        self.chk_load = QCheckBox("Load outputs into project"); self.chk_load.setChecked(True)
        flags = QWidget(); fl = QHBoxLayout(flags); fl.setContentsMargins(0,0,0,0)
        fl.addWidget(self.chk_overwrite); fl.addWidget(self.chk_load); fl.addStretch(1)
        grid.addWidget(flags, 11, 0, 1, 3)

        # Row 12: Run
        run_bar = QWidget(); hb4 = QHBoxLayout(run_bar); hb4.setContentsMargins(0,0,0,0)
        self.btn_run = QPushButton("Run"); self.btn_run.clicked.connect(self.run_convert)
        hb4.addStretch(1); hb4.addWidget(self.btn_run)
        grid.addWidget(run_bar, 12, 0, 1, 3)

        # Row 13: Log/HTML
        self.out_html = QTextBrowser(); self.out_html.setOpenExternalLinks(True)
        grid.addWidget(self.out_html, 13, 0, 1, 3)

        self.driver.currentIndexChanged.connect(self.on_driver_changed)
        self.on_driver_changed()

        lay = QVBoxLayout(self); lay.addLayout(grid)

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

    def scan_layers(self):
        self.layer_list.clear()
        cad_path = self.in_edit.text().strip()
        if not cad_path or not os.path.isfile(cad_path):
            self.log("<span style='color:#b00'>Please pick a valid DXF/DWG first.</span>")
            return
        try:
            from .services import deps as _deps
            _deps.ensure_ezdxf_safe(feedback=SimpleFeedback(self))
        except Exception as e:
            self.log(f"<span style='color:#b00'>Failed to prepare ezdxf: {e}</span>")
            return

        temp_dir = None
        try:
            src_for_scan = cad_path
            if cad_path.lower().endswith(".dwg"):
                self.log("Converting DWG → temporary DXF for layer scan ...")
                src_for_scan = dwg_to_temp_dxf_auto(cad_path, prefer=self.dwg_pref.currentText(),
                                                    dxf_version=(self.dxf_version.text().strip() or "ACAD2013"))
                temp_dir = os.path.dirname(src_for_scan)

            import ezdxf
            self.log("<b>Reading layers ...</b>")
            doc = ezdxf.readfile(src_for_scan)
            names = sorted([str(t.dxf.name) for t in doc.layers])
            if not names:
                self.log("<i>No layers found.</i>")
            else:
                for nm in names:
                    it = QListWidgetItem(nm); it.setCheckState(Qt.Unchecked)
                    self.layer_list.addItem(it)
                self.log(f"Found {len(names)} layer(s).")
        except Exception as e:
            tb = traceback.format_exc()
            self.log(f"""<div style='color:#b00'><b>Layer scan failed:</b> {e}</div>""")
            self.log(f"""<pre>{tb}</pre>""")
        finally:
            if temp_dir and os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def copy_selected_layers(self):
        selected = set([it.text() for it in self.layer_list.selectedItems()])
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            if it.checkState() == Qt.Checked:
                selected.add(it.text())
        if not selected:
            selected = [self.layer_list.item(i).text() for i in range(self.layer_list.count())]
        else:
            selected = sorted(selected)
        self.layers_edit.setText(", ".join(selected))

    def run_convert(self):
        try:
            self.btn_run.setEnabled(False)
            self.log("<b>Checking dependency: ezdxf</b>")
            _deps.ensure_ezdxf_safe(feedback=SimpleFeedback(self))

            from .services.conversion_service import precise_convert, write_outputs

            cad_path = self.in_edit.text().strip()
            if not cad_path or not os.path.isfile(cad_path):
                raise RuntimeError(f"File not found: {cad_path}")

            layers_csv = self.layers_edit.text().strip()
            target_layers = [s.strip() for s in layers_csv.split(',') if s.strip()] if layers_csv else None
            src_epsg = int(self.src_epsg.value())
            tgt_epsg_text = self.tgt_epsg.text().strip()
            tgt_epsg = int(tgt_epsg_text) if tgt_epsg_text else None
            mode = self.block_mode.currentText()
            merge_tol = float(self.merge_tol.value())
            driver = self.driver.currentText()
            out_path = self.out_edit.text().strip()
            if not out_path:
                raise RuntimeError("Please specify output path.")
            dwg_pref = self.dwg_pref.currentText()
            dxf_version = self.dxf_version.text().strip() or "ACAD2013"
            overwrite = self.chk_overwrite.isChecked()
            do_load = self.chk_load.isChecked()

            input_for_convert = cad_path
            temp_dir = None
            if cad_path.lower().endswith(".dwg"):
                self.log("Converting DWG → temporary DXF ...")
                temp_dxf = dwg_to_temp_dxf_auto(cad_path, prefer=dwg_pref, dxf_version=dxf_version)
                input_for_convert = temp_dxf
                temp_dir = os.path.dirname(temp_dxf)

            try:
                self.log("<b>Running conversion ...</b>")
                buckets = precise_convert(
                    [input_for_convert],
                    source_epsg=src_epsg,
                    target_epsg=tgt_epsg,
                    include_3d=False,
                    bbox_wgs84=None,
                    target_layers=target_layers,
                    block_mode=mode,
                    line_merge_tol=merge_tol,
                    fallback_explode_lines=True,
                    on_progress=lambda s: self.log(s or "")
                )
                self.log("<b>Writing outputs ...</b>")
                written = write_outputs(
                    buckets,
                    out_path=out_path,
                    driver="GPKG" if driver=="GPKG" else "ESRI Shapefile",
                    overwrite=overwrite,
                    on_progress=lambda s: self.log(s or "")
                )
            finally:
                if temp_dir and os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)

            if do_load:
                for w in (written or []):
                    path = w.get("path"); name = w.get("layer")
                    uri = path if driver == "ESRI Shapefile" else f"{path}|layername={name}"
                    v = QgsVectorLayer(uri, name, "ogr")
                    if v.isValid():
                        QgsProject.instance().addMapLayer(v)

            html = "<h3>CAD to GIS Converter</h3><ul>" + "\n".join(
                [f"<li><b>{w.get('layer')}</b> ({w.get('count')}) → {w.get('path')}</li>" for w in (written or [])]
            ) + "</ul>"
            self.out_html.append(html)
            self.log("<b>Done.</b>")
        except Exception as e:
            tb = traceback.format_exc()
            self.out_html.append(f"""<div style='color:#b00'><b>ERROR:</b> {e}</div><pre>{tb}</pre>""")
        finally:
            self.btn_run.setEnabled(True)

class SimpleFeedback:
    def __init__(self, dlg: CadToGisDialog):
        self.dlg = dlg
    def pushInfo(self, s): self.dlg.log(s or "")
