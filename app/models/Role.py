from tortoise import models, fields


class Role(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
    description = fields.TextField(null=True)
    server = fields.ForeignKeyField(
        "models.Server", related_name="roles")
    created_at = fields.DatetimeField(auto_now_add=True)
    settings = fields.JSONField(default=dict)

    class Meta:
        table = "roles"
        unique_together = (("name", "server"),)
