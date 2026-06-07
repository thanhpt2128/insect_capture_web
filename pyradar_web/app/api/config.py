from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Query

from app.core.settings import CONFIG_DIR
from app.services.radar_metrics import calculate_radar_metrics

router = APIRouter()


def _resolve_cfg_path(path_raw: str) -> Path:
    if not path_raw or not path_raw.strip():
        raise ValueError("path is required")

    path_raw = path_raw.strip().strip("\"'")
    path = Path(path_raw)

    if not path.is_absolute():
        # Relative paths are resolved against the default config directory.
        path = (CONFIG_DIR / path).resolve()

    return path


@router.get("/list")
def list_cfg_files() -> dict:
    base = CONFIG_DIR
    if not base.exists() or not base.is_dir():
        return {"base_dir": str(base), "files": []}

    files: List[dict] = []
    for item in sorted(base.glob("*.cfg")):
        try:
            size = item.stat().st_size
        except OSError:
            size = None

        files.append(
            {
                "name": item.name,
                "path": str(item),
                "size": size,
                "relative": item.name,
            }
        )

    return {"base_dir": str(base), "files": files}


@router.get("/metrics")
def get_metrics(path: str = Query(..., description="Absolute path or filename under CONFIG_DIR")):
    try:
        cfg_path = _resolve_cfg_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not cfg_path.exists():
        raise HTTPException(status_code=404, detail=f"CFG file not found: {cfg_path}")

    if cfg_path.suffix.lower() != ".cfg":
        raise HTTPException(status_code=400, detail=f"Unsupported config format: {cfg_path.suffix}")

    try:
        metrics = calculate_radar_metrics(cfg_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"cfg_path": str(cfg_path), "metrics": metrics}
