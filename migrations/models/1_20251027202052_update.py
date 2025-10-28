from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "server" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "server_profile" JSONB NOT NULL,
    "server_settings" JSONB NOT NULL
);
        CREATE TABLE IF NOT EXISTS "roles" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(50) NOT NULL UNIQUE,
    "description" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "settings" JSONB NOT NULL,
    "server_id" INT NOT NULL REFERENCES "server" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_roles_name_ff0ce1" UNIQUE ("name", "server_id")
);
        CREATE TABLE IF NOT EXISTS "users" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "username" VARCHAR(50) NOT NULL UNIQUE,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "is_active" BOOL NOT NULL DEFAULT True,
    "public_key" TEXT,
    "profile" JSONB NOT NULL
);
        CREATE TABLE IF NOT EXISTS "role_to_user" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "assigned_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "role_id" INT NOT NULL REFERENCES "roles" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_role_to_use_role_id_3022f8" UNIQUE ("role_id", "user_id")
);
        CREATE TABLE IF NOT EXISTS "usertoserver" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "server_id" INT NOT NULL REFERENCES "server" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "server";
        DROP TABLE IF EXISTS "roles";
        DROP TABLE IF EXISTS "role_to_user";
        DROP TABLE IF EXISTS "usertoserver";
        DROP TABLE IF EXISTS "users";"""


MODELS_STATE = (
    "eJztm11T2zgUhv9KxlfsTNuhWWg7e5dA2KaFpANmt1OG8Si2kmhwpNSWgQyT/76S/CXLH0"
    "1Se4lBd+ToHFt6pGO9PjJPxoI40PXfXRIXGn91ngwMFvyPjP1NxwDLZWrlBgomIsLwmIew"
    "gIlPPWBTZpwC14fM5EDf9tCSIoKZFQeuy43EZo4Iz1JTgNHPAFqUzCCdQ4813NyEPWGNPv"
    "Tume32lv2NsAMf2e2YA/+5vLOmCLpOpufI4VHCbtHVUtiGmJ4JR37/iWUTN1jg1Hm5onOC"
    "E2+EKbfOIIYeoJBfnnoBHxDvbzTweIxh31OXsItSjAOnIHCpBGBDKjbBnCjrjS8GOON3ed"
    "t9f/Tx6NOfH44+MRfRk8TycR0OLx17GCgIjExjLdoBBaGHwJhyi3lnyZ3MgVeMLvZX4LEu"
    "q/BiVM9KbwEeLRfiGZ2zn8eHFaj+6V2efO5dHhwf/sFHQtiiDpf6KGrpiiZOM6UndysH0Y"
    "SPJetPCduJZUQqQRm7pCzTfKwHZgU8c/Dd5H1e+P5PV4Z2cNH7LnguVlHL+Xj0d+wuQT45"
    "H/cVuLYH+fAtQPNsT1kLRQtYzDcbqeB1otB38R+7LNzmaRtsDM4Yu6torqvoDy8GV2bv4l"
    "tmCk575oC3dDP4Y+vBB2WZJxfp/Ds0P3f4z86P8WggCBKfzjxxx9TP/GHwPoGAEguTBws4"
    "UgLH1hhMZmJ9SCkbup+f1i9X41HxlMoxyoReY4b2xkE2fdNxkU9vm5pQ42ltNJI+fNjV6a"
    "NmijIp/AJq+oQ7qLXVxpiJ+fX+uCeZUsMWyXXF9K5wh4yUSA7iGfEgmuGvcCVYDlmfALaL"
    "NsdIVV0lF9o/hut4JcTWNJU98JDorewCYUNkA4M0lAy9q5Pe6cAQKCfAvnsAnmOVMOXy0Q"
    "rYxQqeAf0o9uzrJXRByR4pCVWTXPttwyogkS6R4GSw5ZsW3YVqARjMRK/5vfmd8lBKtH2K"
    "rFrhM20uZqkJoe9F7xji+lrmNyzzge+zZ9VOYkoJ1Wpqz9SUSNSt8kKKeE17vAyNP3W2gy"
    "ZFvCZoFcIofoL/piyKi037x29TUSTlU7EkUldeDdTap3lUalJCbSskm1RPkUovUE6pfi9X"
    "TenLQr16Seujl1wG/R+2gkwh9P3hJpVQ5lVaChVtulz3KgRm9M6/9MgUFe34VUU7NVKX7r"
    "Yo3e1WJ82FauZlzHPidtPaVQ1lq/18jJUWrAqWZw31O65iTdLGwmijFbyy2t2vq3bJnGj5"
    "2Sb5yadtWwkqx+jTeK1AX7ACRb7FHmXoviA/+oTtpQCXPGHkOGVWJyywqYlM8qZu5dMfj8"
    "8zc9Yfqh9SXF/0B+zdTUwWc0JhUSVfAV0GExfZ1h1c5aGWf7mSjdIfrhR+uLLDm5J+RWpK"
    "rosyY02avZVHzbljj1C+a+XelHJPsJQoeBlbtZKnRNeT2ynotRR9oVJUf1WnT9yf7cRdnx"
    "1venacT9kauLVR7ajkfusDziaVUw96yJ4XaaaopVItgdRH66Q9e6BV6ST+IlL4zzPldU8p"
    "pJ2n793j4w0qn8yrtPQp2rLbK0+NLSBG7u0E2MznCwRTiAvkennRRgp5rqJNY4Wx5yja1L"
    "+9rP8DYqr6VQ=="
)
