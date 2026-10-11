import os, shutil
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile, QgsProcessingParameterString,    QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterNumber,    QgsProcessingParameterFileDestination, QgsProcessingParameterFolderDestination, QgsProcessingOutputHtml,    QgsVectorLayer, QgsProject, QgsProcessingException)
from .services import dwg_support as dwg
from .services import deps as _deps

# QGIS 3.36+/4 moved these enums into Qgis.*; the old class enums are gone in QGIS 4.
# The fallbacks use the fully scoped names (PyQt5 >= 5.11 accepts them), which also
# satisfies the plugin repository's QGIS 4 enum check.
try:
    from qgis.core import Qgis
    _INT = Qgis.ProcessingNumberParameterType.Integer
    _DBL = Qgis.ProcessingNumberParameterType.Double
    _NO_THREADING = Qgis.ProcessingAlgorithmFlag.NoThreading
except (ImportError, AttributeError):          # QGIS 3.22 - 3.34
    _INT = QgsProcessingParameterNumber.Type.Integer
    _DBL = QgsProcessingParameterNumber.Type.Double
    _NO_THREADING = QgsProcessingAlgorithm.Flag.FlagNoThreading

class AlgCadToGisConvert(QgsProcessingAlgorithm):
    P_INPUT='INPUT'; P_LAYERS='LAYERS'; P_SRC_EPSG='SRC_EPSG'; P_TGT_EPSG='TGT_EPSG'; P_MODE='MODE'; P_MERGE_TOL='MERGE_TOL'    ; P_DRIVER='DRIVER'; P_OUT_GPKG='OUT_GPKG'; P_OUT_FOLDER='OUT_FOLDER'; P_OVERWRITE='OVERWRITE'; P_LOAD='LOAD'; P_DWG_PREFER='DWG_PREFER'; P_DXF_VERSION='DXF_VERSION'
    O_SUMMARY='SUMMARY'

    def name(self): return 'cad_to_gis_convert'
    def displayName(self): return 'CAD to GIS Converter'
    def group(self): return 'CAD Tools'
    def groupId(self): return 'cad_tools'
    def shortHelpString(self): return 'Convert DXF/DWG into GIS layers grouped by CAD layers.'

    # Required for every Python Processing algorithm -- without it QGIS raises
    # "createInstance() is not implemented" as soon as the tool is opened.
    def createInstance(self): return AlgCadToGisConvert()

    # Output layers are added to the project at the end of processAlgorithm();
    # QgsProject must only be touched from the main thread.
    def flags(self): return super().flags() | _NO_THREADING

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(self.P_INPUT,'Input CAD file (DXF or DWG)'))
        self.addParameter(QgsProcessingParameterString(self.P_LAYERS,'Layer names (CSV, optional; leave empty for ALL)', optional=True))
        self.addParameter(QgsProcessingParameterNumber(self.P_SRC_EPSG,'Source EPSG', type=_INT, defaultValue=3826))
        self.addParameter(QgsProcessingParameterString(self.P_TGT_EPSG,'Target EPSG (optional)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.P_MODE,'Block handling mode', options=['keep-merge','explode'], defaultValue=0))
        # Default merge tolerance = 0.0
        self.addParameter(QgsProcessingParameterNumber(self.P_MERGE_TOL,'Line-merge tolerance', type=_DBL, defaultValue=0.0))
        self.addParameter(QgsProcessingParameterEnum(self.P_DRIVER,'Output driver', options=['GPKG','ESRI Shapefile'], defaultValue=0))
        self.addParameter(QgsProcessingParameterFileDestination(self.P_OUT_GPKG,'Output GeoPackage (for driver=GPKG)', fileFilter='GeoPackage (*.gpkg)', optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination(self.P_OUT_FOLDER,'Output folder (for driver=SHP)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.P_DWG_PREFER,'DWG converter preference', options=['auto','oda','libredwg'], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(self.P_DXF_VERSION,'DXF version for DWG conversion', defaultValue='ACAD2013', optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.P_OVERWRITE,'Overwrite existing', defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(self.P_LOAD,'Load outputs into project', defaultValue=True))
        self.addOutput(QgsProcessingOutputHtml(self.O_SUMMARY,'Conversion summary'))

    def processAlgorithm(self, parameters, context, feedback):
        dep = _deps.check_dependencies()
        if not dep["ok"]:
            raise QgsProcessingException(_deps.install_instructions(dep["missing_required"]))
        _deps.ensure_ezdxf_safe(feedback)

        from .services.conversion_service import precise_convert, write_outputs

        cad_path = self.parameterAsFile(parameters, self.P_INPUT, context)
        if not cad_path or not os.path.isfile(cad_path):
            raise QgsProcessingException(f'File not found: {cad_path}')

        layers_csv = self.parameterAsString(parameters, self.P_LAYERS, context).strip()
        target_layers = [s.strip() for s in layers_csv.split(',') if s.strip()] if layers_csv else None
        src_epsg = int(self.parameterAsInt(parameters, self.P_SRC_EPSG, context))
        tgt_epsg_text = self.parameterAsString(parameters, self.P_TGT_EPSG, context).strip()
        tgt_epsg = int(tgt_epsg_text) if tgt_epsg_text else None
        mode = ['keep-merge','explode'][int(self.parameterAsEnum(parameters, self.P_MODE, context))]
        merge_tol = float(self.parameterAsDouble(parameters, self.P_MERGE_TOL, context))
        driver = ['GPKG','ESRI Shapefile'][int(self.parameterAsEnum(parameters, self.P_DRIVER, context))]
        out_gpkg = self.parameterAsFileOutput(parameters, self.P_OUT_GPKG, context)
        out_folder = self.parameterAsString(parameters, self.P_OUT_FOLDER, context)
        dwg_pref = ['auto','oda','libredwg'][int(self.parameterAsEnum(parameters, self.P_DWG_PREFER, context))]
        dxf_version = self.parameterAsString(parameters, self.P_DXF_VERSION, context).strip() or 'ACAD2013'
        overwrite = bool(self.parameterAsBool(parameters, self.P_OVERWRITE, context))
        do_load = bool(self.parameterAsBool(parameters, self.P_LOAD, context))

        input_for_convert = cad_path
        temp_dir = None
        if cad_path.lower().endswith('.dwg'):
            # ODA -> LibreDWG -> GDAL CAD driver (R2000); otherwise explain what to install
            try:
                kind, p = dwg.open_dwg(cad_path, prefer=dwg_pref, dxf_version=dxf_version,
                                       on_progress=feedback.pushInfo)
            except dwg.DwgUnavailableError as ex:
                raise QgsProcessingException(ex.guidance)
            input_for_convert = p
            if kind == 'dxf':
                temp_dir = os.path.dirname(p)
            else:
                feedback.reportError('No ODA / LibreDWG: DWG R2000 read directly with GDAL '
                                     '(blocks not merged; some entities may be missing).', fatalError=False)

        try:
            buckets = precise_convert([input_for_convert], source_epsg=src_epsg, target_epsg=tgt_epsg,
                                      include_3d=False, bbox_wgs84=None, target_layers=target_layers,
                                      block_mode=mode, merge_tolerance=merge_tol,
                                      on_progress=feedback.pushInfo)
            out_path = out_gpkg if driver=='GPKG' else out_folder
            if not out_path:
                raise QgsProcessingException('Please specify output path (GPKG or folder)')
            written = write_outputs(buckets, out_path=out_path, driver=driver, overwrite=overwrite,
                                    on_progress=feedback.pushInfo)
        finally:
            if temp_dir and os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

        html = '<h3>CAD to GIS Converter</h3><ul>' + '\n'.join(
            [f'<li><b>{w.get("layer")}</b> ({w.get("count")}) → {w.get("path")}</li>' for w in (written or [])]) + '</ul>'
        if do_load:
            for w in (written or []):
                path = w.get('path'); name = w.get('layer')
                uri = path if driver=='ESRI Shapefile' else f"{path}|layername={name}"
                v = QgsVectorLayer(uri, name, 'ogr')
                if v.isValid(): QgsProject.instance().addMapLayer(v)
        return {self.O_SUMMARY: html}
