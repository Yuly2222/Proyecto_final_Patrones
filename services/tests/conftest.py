import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
os.environ.setdefault("ENV", "ci")
sys.path[:0] = [ROOT, os.path.join(ROOT, "gateway")]  # common.py, local_tables.py, saga.py


def load_service(name: str):
    """Cada servicio tiene su propio main.py; se cargan con nombres distintos para no chocar."""
    spec = importlib.util.spec_from_file_location(f"{name}_main", os.path.join(ROOT, name, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
