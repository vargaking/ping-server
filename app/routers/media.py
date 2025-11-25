from fastapi import APIRouter, Request
from app.utils import call_media_server

router = APIRouter(prefix="/api/media", tags=["media"])

@router.get("/router_capabilities")
async def get_router_capabilities_proxy(request: Request):
    return await call_media_server(request, "GET", "/router_capabilities")

@router.post("/create_transport")
async def create_transport_proxy(request: Request, data: dict):
    return await call_media_server(request, "POST", "/create_transport", data)

@router.post("/connect_transport")
async def connect_transport_proxy(request: Request, data: dict):
    return await call_media_server(request, "POST", "/connect_transport", data)

@router.post("/produce")
async def produce_proxy(request: Request, data: dict):
    return await call_media_server(request, "POST", "/produce", data)

@router.post("/consume")
async def consume(request: Request):
    return await call_media_server(request, "POST", "/consume", await request.json())

@router.post("/resume_consumer")
async def resume_consumer(request: Request):
    return await call_media_server(request, "POST", "/resume_consumer", await request.json())
