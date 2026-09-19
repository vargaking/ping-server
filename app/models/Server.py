from tortoise import models, fields


class Server(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)
    server_profile = fields.JSONField(default=dict)
    server_settings = fields.JSONField(default=dict)
    # The user who owns the server. Nullable so a server can outlive its owner
    # (on account deletion the FK is set to NULL rather than cascading).
    owner = fields.ForeignKeyField(
        "models.User", related_name="owned_servers",
        null=True, on_delete=fields.SET_NULL)

    def __str__(self):
        return self.name

    class Meta:
        table = "servers"
        ordering = ["-created_at"]
