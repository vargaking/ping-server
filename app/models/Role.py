from tortoise import models, fields


class Role(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    description = fields.TextField(null=True)
    server = fields.ForeignKeyField(
        "models.Server", related_name="roles")
    # Permission bitmasks (see app.permissions.Permission).
    allow = fields.BigIntField(default=0)
    deny = fields.BigIntField(default=0)
    parent = fields.ForeignKeyField(
        "models.Role", related_name="children",
        null=True, on_delete=fields.SET_NULL)
    is_default = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    settings = fields.JSONField(default=dict)

    class Meta:
        table = "roles"
        unique_together = (("name", "server"),)
