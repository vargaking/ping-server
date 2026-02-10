import os

TORTOISE_CONFIG = {
    "connections": {"default": os.getenv("DB_CONNECTION_STRING")},
    "apps": {
        "models": {
            "models": ["app.models", "aerich.models"],
            "default_connection": "default",
        },
    },
}
