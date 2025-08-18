import os, shutil
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile, QgsProcessingParameterString,    QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterNumber,    QgsProcessingParameterFileDestination, QgsProcessingParameterFolderDestination, QgsProcessingOutputHtml,    QgsVectorLayer, QgsProject, QgsProcessingException)
from .services.dwg_support import dwg_to_temp_dxf_auto
from .services import deps as _deps

class AlgCadToGisConvert(QgsProcessingAlgorithm):
    P_INPUT='INPUT'; P_LAYERS='LAYERS'; P_SRC_EPSG='SRC_EPSG'; P_TGT_EPSG='TGT_EPSG'; P_MODE='MODE'; P_MERGE_TOL='MERGE_TOL'    ; P_DRIVER='DRIVER'; P_OUT_GPKG='OUT_GPKG'; P_OUT_FOLDER='OUT_FOLDER'; P_OVERWRITE='OVERWRITE'; P_LOAD='LOAD'; P_DWG_PREFER='DWG_PREFER'; P_DXF_VERSION='DXF_VERSION'
    O_SUMMARY='SUMMARY'

    def name(self): return 'cad_to_gis_convert'
    def displayName(self): return 'CAD to GIS Converter'
    def group(self): return 'CAD Tools'
    def groupId(self): return 'cad_tools'
    def shortHelpString(self): return 'Convert DXF/DWG into GIS layers grouped by CAD layers.'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(self.P_INPUT,'Input CAD file (DXF or DWG)'))
        self.addParameter(QgsProcessingParameterString(self.P_LAYERS,'Layer names (CSV, optional; leave empty for ALL)', optional=True))
        self.addParameter(QgsProcessingParameterNumber(self.P_SRC_EPSG,'Source EPSG', type=QgsProcessingParameterNumber.Integer, defaultValue=3826))
        self.addParameter(QgsProcessingParameterString(self.P_TGT_EPSG,'Target EPSG (optional)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.P_MODE,'Block handling mode', options=['keep-merge','explode'], defaultValue=0))
        # Default merge tolerance = 0.0
        self.addParameter(QgsProcessingParameterNumber(self.P_MERGE_TOL,'Line-merge tolerance', type=QgsProcessingParameterNumber.Double, defaultValue=0.0))
        self.addParameter(QgsProcessingParameterEnum(self.P_DRIVER,'Output driver', options=['GPKG','ESRI Shapefile'], defaultValue=0))
        self.addParameter(QgsProcessingParameterFileDestination(self.P_OUT_GPKG,'Output GeoPackage (for driver=GPKG)', fileFilter='GeoPackage (*.gpkg)', optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination(self.P_OUT_FOLDER,'Output folder (for driver=SHP)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.P_DWG_PREFER,'DWG converter preference', options=['auto','oda','libredwg'], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(self.P_DXF_VERSION,'DXF version for DWG conversion', defaultValue='ACAD2013', optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.P_OVERWRITE,'Overwrite existing', defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(self.P_LOAD,'Load outputs into project', defaultValue=True))
        self.addOutput(QgsProcessingOutputHtml(self.O_SUMMARY,'Conversion summary'))

    def processAlgorithm(self, parameters, context, feedback):
        # Ensure ezdxf safely
        try:
            _deps.ensure_ezdxf_safe(feedback)
        except Exception as ex:
            raise QgsProcessingException(f"Failed to ensure ezdxf is installed: {ex}")

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
        out_folder = self.parameterAsFile(parameters, self.P_OUT_FOLDER, context)
        dwg_pref = ['auto','oda','libredwg'][int(self.parameterAsEnum(parameters, self.P_DWG_PREFER, context))]
        dxf_version = self.parameterAsString(parameters, self.P_DXF_VERSION, context).strip() or 'ACAD2013'
        overwrite = bool(self.parameterAsBool(parameters, self.P_OVERWRITE, context))
        do_load = bool(self.parameterAsBool(parameters, self.P_LOAD, context))

        input_for_convert = cad_path
        temp_dir = None
        if cad_path.lower().endswith('.dwg'):
            temp_dxf = dwg_to_temp_dxf_auto(cad_path, prefer=dwg_pref, dxf_version=dxf_version)
            input_for_convert = temp_dxf
            temp_dir = os.path.dirname(temp_dxf)

        try:
            buckets = precise_convert([input_for_convert], source_epsg=src_epsg, target_epsg=tgt_epsg,
                                      include_3d=False, bbox_wgs84=None, target_layers=target_layers,
                                      block_mode=mode, line_merge_tol=merge_tol, fallback_explode_lines=True,
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
