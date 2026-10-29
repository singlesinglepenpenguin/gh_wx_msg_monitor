"""Central locations for source execution and bundled applications."""
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resource_root():
    return Path(getattr(sys, '_MEIPASS', PROJECT_ROOT))


def runtime_root():
    if not getattr(sys, 'frozen', False):
        return PROJECT_ROOT
    base = Path(sys.executable).resolve().parent
    if sys.platform == 'darwin':
        for parent in base.parents:
            if parent.suffix == '.app':
                return parent.parent
    return base


def data_home():
    return runtime_root() / 'data/local_data'
