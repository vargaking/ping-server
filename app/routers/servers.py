from fastapi import APIRouter, HTTPException, status
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
from app.models.Server import Server

router = APIRouter(prefix="/servers", tags=["servers"])


class ServerCreate(BaseModel):
    name: str
    server_profile: dict = {}
    server_settings: dict = {}


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    server_profile: Optional[dict] = None
    server_settings: Optional[dict] = None


class ServerResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    server_profile: dict
    server_settings: dict

    @classmethod
    def from_server(cls, server: Server):
        return cls(
            id=server.id,
            name=server.name,
            created_at=server.created_at,
            server_profile=server.server_profile,
            server_settings=server.server_settings
        )


@router.post("/", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(server: ServerCreate):
    server_obj = await Server.create(**server.model_dump())
    return ServerResponse.from_server(server_obj)


@router.get("/", response_model=List[ServerResponse])
async def get_servers():
    servers = await Server.all()
    return [ServerResponse.from_server(server) for server in servers]


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(server_id: int):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return ServerResponse.from_server(server)


@router.put("/{server_id}", response_model=ServerResponse)
async def update_server(server_id: int, server_update: ServerUpdate):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    update_data = server_update.model_dump(exclude_unset=True)
    await server.update_from_dict(update_data)
    await server.save()
    return ServerResponse.from_server(server)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: int):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    await server.delete()