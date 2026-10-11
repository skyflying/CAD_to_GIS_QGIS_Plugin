# -*- coding: utf-8 -*-
"""
Python dependency check / optional installer for the QGIS plugin.

QGIS does not ship geopandas (and, depending on the installer, not pandas/pyproj
either), so the plugin must load without them and explain how to get them.
Nothing here imports the heavy packages; find_spec() only looks them up.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import site
import subprocess  # nosec B404 - only runs QGIS's own python -m pip, list args, no shell
import sys
from typing import Callable, Dict, List, Optional
import logging

_log = logging.getLogger(__name__)

# (import name, pip name)
REQUIRED = [("shapely", "shapely"), ("pandas", "pandas"), ("pyproj", "pyproj"), ("geopandas", "geopandas")]
IO_BACKENDS = [("pyogrio", "pyogrio"), ("fiona", "fiona")]      # geopandas needs one of them
OPTIONAL = [("ezdxf", "ezdxf")]                                   # without it: GDAL/OGR pipeline


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def check_dependencies() -> Dict[str, object]:
    missing_req = [pip for mod, pip in REQUIRED if not _has(mod)]
    if not any(_has(m) for m, _p in IO_BACKENDS):
        missing_req.append("pyogrio")
    missing_opt = [pip for mod, pip in OPTIONAL if not _has(mod)]
    ok = not missing_req
    if ok and not missing_opt:
        summary = "✔ Python packages OK"
    elif ok:
        summary = "✔ OK (optional missing: " + ", ".join(missing_opt) + " → GDAL/OGR pipeline is used)"
    else:
        summary = "✖ Missing: " + ", ".join(missing_req)
    return {"ok": ok, "missing_required": missing_req, "missing_optional": missing_opt, "summary": summary}


def ensure_ezdxf_safe(feedback=None):
    """Backward compatible helper: True if ezdxf can be imported."""
    try:
        import ezdxf  # noqa: F401
        if feedback:
            feedback.pushInfo("ezdxf available.")
        return True
    except Exception as e:
        if feedback:
            feedback.pushInfo(f"ezdxf not available: {e}. Will use OGR fallback.")
        return False


def install_instructions(packages: List[str]) -> str:
    pk = " ".join(packages) or "geopandas ezdxf"
    return (
        "This plugin needs these Python packages inside QGIS's own Python:\n"
        f"    {pk}\n\n"
        "Windows (QGIS standalone installer or OSGeo4W):\n"
        "  1. Start menu → 'OSGeo4W Shell' (run as administrator for an all-users install)\n"
        f"  2. python -m pip install {pk}\n"
        "  3. Restart QGIS\n"
        "  (OSGeo4W users can alternatively pick the python3-* packages in OSGeo4W Setup.)\n\n"
        "macOS (official QGIS.app):\n"
        f"  /Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install --user {pk}\n\n"
        "Linux: use your distribution packages, e.g.\n"
        "  sudo apt install python3-geopandas python3-ezdxf   (Debian/Ubuntu)\n"
        "  sudo dnf install python3-geopandas python3-ezdxf   (Fedora)\n"
    )


def pip_install_supported() -> bool:
    # Linux distro Pythons are "externally managed" (PEP 668) → use the package manager
    return os.name == "nt" or sys.platform == "darwin"


def qgis_python_executable() -> Optional[str]:
    """Locate the python interpreter that belongs to the running QGIS
    (sys.executable is qgis-bin.exe / QGIS.app, not python)."""
    if os.name == "nt":
        for c in (os.path.join(sys.exec_prefix, "python.exe"), os.path.join(sys.exec_prefix, "python3.exe")):
            if os.path.isfile(c):
                return c
    elif sys.platform == "darwin":
        c = os.path.join(os.path.dirname(sys.executable), "bin", "python3")
        if os.path.isfile(c):
            return c
    if "python" in os.path.basename(sys.executable).lower():
        return sys.executable
    return shutil.which("python3") or shutil.which("python")


def pip_install(packages: List[str], on_output: Optional[Callable[[str], None]] = None) -> int:
    """Run `python -m pip install --user <packages>` with QGIS's interpreter.

    Only call after the user agreed. Streams pip output to on_output.
    Returns pip's exit code. Newly created user site-packages are added to sys.path
    so the packages can often be used without restarting QGIS.
    """
    allowed = {pip for _m, pip in REQUIRED + IO_BACKENDS + OPTIONAL}
    bad = [p for p in packages if p not in allowed]
    if bad:                                   # never pass arbitrary text to pip
        raise ValueError("Refusing to install unexpected package(s): " + ", ".join(bad))
    py = qgis_python_executable()
    if not py or not os.path.isabs(py) or not os.path.isfile(py):
        raise RuntimeError("Cannot locate QGIS's Python interpreter.")
    cmd = [py, "-m", "pip", "install", "--user", "--disable-pip-version-check", *packages]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
    env = dict(os.environ)
    if on_output:
        on_output("$ " + " ".join(cmd))
    # Fixed argv (interpreter path + allow-listed package names), shell=False.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env, **kw)  # nosec B603
    for line in (proc.stdout or []):
        if on_output:
            on_output(line.rstrip())
    rc = proc.wait()
    try:
        user_site = site.getusersitepackages()
        if os.path.isdir(user_site) and user_site not in sys.path:
            site.addsitedir(user_site)
        importlib.invalidate_caches()
    except Exception:
        _log.debug("pip_install: ignored exception", exc_info=True)
    return rc
