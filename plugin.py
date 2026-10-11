# -*- coding: utf-8 -*-
"""
plugin.py - QGIS plugin entry for CAD To GIS Convert
Updated to pass a QWidget parent (iface.mainWindow()) to the dialog.
"""

import os

try:                                    # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:                     # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import QgsApplication

from .ui_dialog import CadToGisDialog

_ICON = os.path.join(os.path.dirname(__file__), "icons", "icon.png")
_MENU = "&CAD To GIS Convert"


class CadToGisPlugin:
    def __init__(self, iface):
        """Constructor.
        :param iface: QgisInterface
        """
        self.iface = iface
        self.action = None
        self.dlg = None
        self.provider = None

    def initProcessing(self):
        """metadata.txt declares hasProcessingProvider=yes, but the provider was never
        registered, so the Processing algorithm never showed up in the toolbox."""
        from .provider import CadToGisProvider
        self.provider = CadToGisProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""
        self.initProcessing()
        # ":/plugins/.../icon.png" pointed to a Qt resource that is never compiled
        self.action = QAction(QIcon(_ICON),
                              QCoreApplication.translate("CadToGisPlugin", "CAD To GIS Convert"),
                              self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addPluginToVectorMenu(_MENU, self.action)    # Vector menu (repository guideline)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        """Removes the plugin menu item and icon from QGIS GUI."""
        if self.action:
            self.iface.removePluginVectorMenu(_MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

    def show_dialog(self):
        """Show the main dialog."""
        parent = self.iface.mainWindow()
        if self.dlg is None:
            self.dlg = CadToGisDialog(parent)  # parent must be QWidget
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
