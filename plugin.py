# -*- coding: utf-8 -*-
"""
plugin.py - QGIS plugin entry for CAD To GIS Convert
Updated to pass a QWidget parent (iface.mainWindow()) to the dialog.
"""

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QCoreApplication

from .ui_dialog import CadToGisDialog


class CadToGisPlugin:
    def __init__(self, iface):
        """Constructor.
        :param iface: QgisInterface
        """
        self.iface = iface
        self.action = None
        self.dlg = None

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""
        self.action = QAction(QIcon(":/plugins/cad_to_gis_convert/icon.png"),
                              QCoreApplication.translate("CadToGisPlugin", "CAD To GIS Convert"),
                              self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addPluginToMenu("&CAD To GIS Convert", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        """Removes the plugin menu item and icon from QGIS GUI."""
        if self.action:
            self.iface.removePluginMenu("&CAD To GIS Convert", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    def show_dialog(self):
        """Show the main dialog."""
        parent = self.iface.mainWindow()
        if self.dlg is None:
            self.dlg = CadToGisDialog(parent)  # parent must be QWidget
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
