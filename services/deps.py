from __future__ import annotations
import sys, os, subprocess, importlib
from pathlib import Path

def _find_qgis_python_exe() -> str:
    exe = sys.executable or ""
    if exe.lower().endswith(("python.exe","pythonw.exe")) and os.path.isfile(exe):
        return exe
    qgis_bin = Path(exe)
    root = qgis_bin.parent.parent if qgis_bin.exists() else None
    if root and root.is_dir():
        for ver in ("Python39","Python310","Python311","Python312"):
            cand = root / "apps" / ver / "python.exe"
            if cand.exists():
                return str(cand)
    return "python"

def _ensure_pip(pyexe: str, feedback=None):
    try:
        res = subprocess.run([pyexe, "-m", "pip", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if res.returncode == 0:
            return
    except Exception:
        pass
    res = subprocess.run([pyexe, "-m", "ensurepip", "--upgrade"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if feedback:
        feedback.pushInfo(res.stdout[-1000:])

def _pip_install(pkg: str, feedback=None, use_user=True):
    pyexe = _find_qgis_python_exe()
    if feedback:
        feedback.pushInfo(f"[deps] Python used for pip: {pyexe}")
    _ensure_pip(pyexe, feedback)
    cmd = [pyexe, "-m", "pip", "install"]
    if use_user:
        cmd.append("--user")
    cmd.append(pkg)
    if feedback:
        feedback.pushInfo(f"[deps] Running: {' '.join(cmd)}")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if feedback:
        feedback.pushInfo(res.stdout[-2000:])
    if res.returncode != 0 and use_user:
        cmd = [pyexe, "-m", "pip", "install", pkg]
        if feedback:
            feedback.pushInfo(f"[deps] Retrying: {' '.join(cmd)}")
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if feedback:
            feedback.pushInfo(res.stdout[-2000:])
    if res.returncode != 0:
        raise RuntimeError(f"pip install {pkg} failed with code {res.returncode}")

def ensure_ezdxf_safe(feedback=None):
    try:
        importlib.import_module("ezdxf")
        return
    except Exception:
        _pip_install("ezdxf", feedback, use_user=True)
        importlib.invalidate_caches()
        importlib.import_module("ezdxf")
        if feedback:
            feedback.pushInfo("[deps] ezdxf is ready")
