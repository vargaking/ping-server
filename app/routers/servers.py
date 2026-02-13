from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from pydantic import BaseModel

from ..middleware import get_current_user
from ..models.Server import Server
from ..models.User import User
from ..models.UserToServer import UserToServer
from ..services.storage import storage_service

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
async def create_server(server: ServerCreate, current_user: User = Depends(get_current_user)):
    server_obj = await Server.create(**server.model_dump())
    # Automatically add the creator as a member
    await UserToServer.create(user=current_user, server=server_obj)
    return ServerResponse.from_server(server_obj)


@router.get("/", response_model=List[ServerResponse])
async def get_servers(current_user: User = Depends(get_current_user)):
    servers = await Server.all()
    return [ServerResponse.from_server(server) for server in servers]


@router.get("/me", response_model=List[ServerResponse])
async def get_my_servers(current_user: User = Depends(get_current_user)):
    # Return servers the current user belongs to
    user_server_relations = await UserToServer.filter(user=current_user).prefetch_related("server")
    servers = [relation.server for relation in user_server_relations]
    return [ServerResponse.from_server(server) for server in servers]


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(server_id: int, current_user: User = Depends(get_current_user)):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return ServerResponse.from_server(server)


@router.put("/{server_id}", response_model=ServerResponse)
async def update_server(server_id: int, server_update: ServerUpdate, current_user: User = Depends(get_current_user)):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    membership = await UserToServer.filter(user=current_user, server=server).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this server")

    update_data = server_update.model_dump(exclude_unset=True)
    await server.update_from_dict(update_data)
    await server.save()
    return ServerResponse.from_server(server)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: int, current_user: User = Depends(get_current_user)):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    membership = await UserToServer.filter(user=current_user, server=server).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this server")

    await server.delete()


@router.post("/{server_id}/icon", response_model=ServerResponse)
async def upload_server_icon(server_id: int, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    server = await Server.get_or_none(id=server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    membership = await UserToServer.filter(user=current_user, server=server).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this server")

    content = await file.read()
    url = await storage_service.upload_file(
        content,
        file.content_type,
        f"servers/{server_id}/icon"
    )

    if not url:
        raise HTTPException(status_code=500, detail="Failed to upload file")

    server.server_profile['icon'] = url
    await server.save()
    return ServerResponse.from_server(server)
