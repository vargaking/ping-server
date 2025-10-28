from fastapi import FastAPI
from tortoise.contrib.fastapi import register_tortoise
from dotenv import load_dotenv
import os
from app.routers import users, servers, auth
from app.middleware import auth_middleware

load_dotenv()

app = FastAPI()

# Add authentication middleware
app.middleware("http")(auth_middleware)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(servers.router)

TORTOISE_CONFIG = {
    "connections": {"default": os.getenv("DB_CONNECTION_STRING")},
    "apps": {
        "models": {
            "models": ["app.models"],
            "default_connection": "default",
        },
    },
}

register_tortoise(
    app,
    config=TORTOISE_CONFIG,
    # Set to True to auto-create tables (not recommended for production)
    generate_schemas=False,
    add_exception_handlers=True,
)


@app.get("/")
async def root():
    return {"message": "Hello World"}
