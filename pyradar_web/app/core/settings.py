from app.core.paths import PYRADAR_ROOT

CONFIG_DIR = (PYRADAR_ROOT / "configFiles").resolve()

DEFAULT_DCA_CONFIG_PATH = (PYRADAR_ROOT / "configFiles" / "cf.json").resolve()
DEFAULT_REALTIME_WORKER = (PYRADAR_ROOT / "realTimeProc_infer.py").resolve()
