import asyncio
from io import BytesIO

import numpy as np
from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect
from matplotlib.figure import Figure

from pydantic import BaseModel, Field

from app.services.realtime_controller import realtime_controller

router = APIRouter()


def _render_png(fig: Figure) -> bytes:
    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=120, bbox_inches="tight")
    return buffer.getvalue()


def _build_placeholder_png(label: str) -> bytes:
    fig = Figure(figsize=(7.2, 2.6), dpi=120, facecolor="#101827")
    ax = fig.subplots()
    ax.set_facecolor("#101827")
    ax.text(0.5, 0.5, label, color="#9ca3af", ha="center", va="center", fontsize=12)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    return _render_png(fig)


def _extract_latest_range_plot():
    range_plot = realtime_controller.get_latest_range_plot()
    if not isinstance(range_plot, dict) or not range_plot.get("ready"):
        return None
    return range_plot


def _profile_png(range_plot: dict) -> bytes:
    profile = np.asarray(range_plot.get("range_profile") or [], dtype=float)
    if profile.size == 0:
        return _build_placeholder_png("No profile data")

    min_db = float(range_plot.get("min_db", np.min(profile)))
    max_db = float(range_plot.get("max_db", np.max(profile)))
    if min_db == max_db:
        min_db -= 1.0
        max_db += 1.0

    fig = Figure(figsize=(7.2, 2.6), dpi=120, facecolor="#101827")
    ax = fig.subplots()
    ax.set_facecolor("#101827")
    ax.plot(np.arange(profile.size), profile, color="#38bdf8", linewidth=1.8)
    ax.set_xlim(0, max(profile.size - 1, 1))
    ax.set_ylim(min_db, max_db)
    ax.set_xlabel("Range bin", color="#cbd5e1")
    ax.set_ylabel("Amplitude (dB)", color="#cbd5e1")
    ax.tick_params(colors="#cbd5e1")
    ax.grid(True, color="#233047", linewidth=0.8)
    for spine in ax.spines.values():
        spine.set_color("#d5dae4")
    fig.tight_layout()
    return _render_png(fig)


def _range_time_png(range_plot: dict) -> bytes:
    matrix = np.asarray(range_plot.get("range_time") or [], dtype=float)
    if matrix.ndim != 2 or matrix.size == 0:
        return _build_placeholder_png("No range-time data")

    min_db = float(range_plot.get("min_db", np.nanmin(matrix)))
    max_db = float(range_plot.get("max_db", np.nanmax(matrix)))
    if min_db == max_db:
        min_db -= 1.0
        max_db += 1.0

    fig = Figure(figsize=(7.2, 3.2), dpi=120, facecolor="#101827")
    ax = fig.subplots()
    ax.set_facecolor("#101827")
    image = ax.imshow(
        matrix.T,
        aspect="auto",
        origin="lower",
        cmap="viridis",
        vmin=min_db,
        vmax=max_db,
        interpolation="nearest",
    )
    ax.set_xlabel("Sliding frame history", color="#cbd5e1")
    ax.set_ylabel("Range bin", color="#cbd5e1")
    ax.tick_params(colors="#cbd5e1")
    for spine in ax.spines.values():
        spine.set_color("#d5dae4")
    colorbar = fig.colorbar(image, ax=ax)
    colorbar.set_label("dB", color="#cbd5e1")
    colorbar.ax.yaxis.set_tick_params(color="#cbd5e1")
    for label in colorbar.ax.get_yticklabels():
        label.set_color("#cbd5e1")
    fig.tight_layout()
    return _render_png(fig)
async def _stream_results(websocket: WebSocket):
    await websocket.accept()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=4)
    loop = asyncio.get_running_loop()

    def on_result(item: dict) -> None:
        def push() -> None:
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass

        loop.call_soon_threadsafe(push)

    listener_id = realtime_controller.add_result_listener(on_result)
    try:
        latest = realtime_controller.get_latest_result()
        if latest:
            await websocket.send_json({"results": [latest]})

        while True:
            item = await queue.get()
            await websocket.send_json({"results": [item]})
    except WebSocketDisconnect:
        return
    finally:
        realtime_controller.remove_result_listener(listener_id)


class StartRequest(BaseModel):
    com_port: str = Field(..., description="Example: COM5")
    cfg_path: str = Field(..., description="Absolute path or filename under configFiles/")
    cli_baud: int = Field(921600, ge=1200, le=4000000)
    numframes: int = Field(30, ge=1, le=512)


@router.post("/start")
def start(payload: StartRequest):
    try:
        return realtime_controller.start(
            com_port=payload.com_port,
            cfg_path=payload.cfg_path,
            cli_baud=payload.cli_baud,
            numframes=payload.numframes,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stop")
def stop():
    try:
        return realtime_controller.stop()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/status")
def status():
    return realtime_controller.status()


@router.get("/results")
def results(tail: int = 50):
    return {"results": realtime_controller.get_results(tail=tail)}


@router.get("/plots/range-profile/latest.png")
def latest_range_profile_plot(_marker: str | None = None):
    range_plot = _extract_latest_range_plot()
    image = _profile_png(range_plot) if range_plot else _build_placeholder_png("Waiting for range profile")
    return Response(content=image, media_type="image/png")


@router.get("/plots/range-time/latest.png")
def latest_range_time_plot(_marker: str | None = None):
    range_plot = _extract_latest_range_plot()
    image = _range_time_png(range_plot) if range_plot else _build_placeholder_png("Waiting for range-time")
    return Response(content=image, media_type="image/png")


@router.websocket("/ws/results")
async def results_ws(websocket: WebSocket):
    await _stream_results(websocket)
