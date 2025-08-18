from qgis.core import QgsProcessingProvider
from .alg_cad_to_gis_convert import AlgCadToGisConvert

class CadToGisProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(AlgCadToGisConvert())

    def id(self): return 'cad_to_gis_convert'
    def name(self): return 'CAD to GIS Converter'
    def longName(self): return 'CAD to GIS Converter (Processing Provider)'
