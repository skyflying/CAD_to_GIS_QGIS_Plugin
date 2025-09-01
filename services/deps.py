
def ensure_ezdxf_safe(feedback=None):
    """
    Try to import ezdxf. If not available, log and return False (we will use OGR fallback).
    """
    try:
        import ezdxf  # noqa: F401
        if feedback:
            feedback.pushInfo("ezdxf available.")
        return True
    except Exception as e:
        if feedback:
            feedback.pushInfo(f"ezdxf not available: {e}. Will use OGR fallback.")
        return False
