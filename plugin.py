from qgis.PyQt.QtWidgets import QAction
from .ui_dialog import CadToGisDialog

class CadToGisPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dlg = None

    def initGui(self):
        self.action = QAction("CAD to GIS Converter", self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addPluginToMenu("CAD to GIS Converter", self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("CAD to GIS Converter", self.action)
            self.action = None
        self.dlg = None

    def show_dialog(self):
        if self.dlg is None:
            self.dlg = CadToGisDialog(self.iface)
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
