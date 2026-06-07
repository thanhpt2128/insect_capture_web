from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
PYRADAR_ROOT = BASE_DIR.parent

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
