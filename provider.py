import os
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider
from .alg_cad_to_gis_convert import AlgCadToGisConvert

class CadToGisProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(AlgCadToGisConvert())

    def id(self): return 'cad_to_gis_convert'
    def name(self): return 'CAD to GIS Converter'
    def longName(self): return 'CAD to GIS Converter (Processing Provider)'
    def icon(self): return QIcon(os.path.join(os.path.dirname(__file__), 'icons', 'icon.png'))
