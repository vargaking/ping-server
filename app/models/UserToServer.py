from tortoise import models, fields


class UserToServer(models.Model):
    user = fields.ForeignKeyField("models.User", related_name="user_servers")
    server = fields.ForeignKeyField(
        "models.Server", related_name="server_users")
    created_at = fields.DatetimeField(auto_now_add=True)
