import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.services.realtime_controller import realtime_controller

router = APIRouter()


def _find_marker(lines, marker):
    if not marker:
        return -1
    marker_ts, marker_id = marker
    for index, item in enumerate(lines):
        if item.get("ts") == marker_ts and item.get("id") == marker_id:
            return index
    return -1


async def _stream_results(websocket: WebSocket, tail: int = 50, interval: float = 0.5):
    await websocket.accept()
    last_marker = None
    try:
        while True:
            lines = realtime_controller.get_results(tail)
            if last_marker is None:
                to_send = lines
            else:
                last_index = _find_marker(lines, last_marker)
                if last_index >= 0:
                    to_send = lines[last_index + 1 :]
                else:
                    to_send = lines

            if to_send:
                await websocket.send_json({"results": to_send})
                last = to_send[-1]
                last_marker = (last.get("ts"), last.get("id"))

            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        return


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


@router.websocket("/ws/results")
async def results_ws(websocket: WebSocket):
    tail_raw = websocket.query_params.get("tail", "50")
    interval_raw = websocket.query_params.get("interval", "0.5")
    try:
        tail = max(10, min(500, int(tail_raw)))
    except ValueError:
        tail = 50

    try:
        interval = float(interval_raw)
    except ValueError:
        interval = 0.5

    if interval < 0.1:
        interval = 0.1

    await _stream_results(websocket, tail=tail, interval=interval)
