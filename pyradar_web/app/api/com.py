from fastapi import APIRouter, HTTPException

from app.services.com_service import com_service

router = APIRouter()


@router.get("/list")
def list_ports():
    try:
        return {"ports": com_service.list_ports()}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
