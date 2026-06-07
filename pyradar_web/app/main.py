from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import app.api.com as com
import app.api.config as config
import app.api.health as health
import app.api.realtime as realtime

app = FastAPI(title="pyRadar Web")

app.mount("/static", StaticFiles(directory="app/ui/static"), name="static")
templates = Jinja2Templates(directory="app/ui/templates")

app.include_router(health.router)
app.include_router(com.router, prefix="/com", tags=["com"])
app.include_router(config.router, prefix="/config", tags=["config"])
app.include_router(realtime.router, prefix="/realtime", tags=["realtime"])


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})
