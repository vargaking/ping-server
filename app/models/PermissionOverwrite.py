from tortoise import fields, models


class PermissionOverwrite(models.Model):
    """What a role or a member may do in one channel or category, on top of the
    server-level roles. Exactly one target and exactly one subject is set."""
    id = fields.IntField(pk=True)
    server = fields.ForeignKeyField(
        "models.Server", related_name="permission_overwrites", on_delete=fields.CASCADE)
    channel = fields.ForeignKeyField(
        "models.Channel", related_name="permission_overwrites", null=True,
        on_delete=fields.CASCADE)
    group = fields.ForeignKeyField(
        "models.ChannelGroup", related_name="permission_overwrites", null=True,
        on_delete=fields.CASCADE)
    role = fields.ForeignKeyField(
        "models.Role", related_name="permission_overwrites", null=True,
        on_delete=fields.CASCADE)
    user = fields.ForeignKeyField(
        "models.User", related_name="permission_overwrites", null=True,
        on_delete=fields.CASCADE)
    allow = fields.BigIntField(default=0)
    deny = fields.BigIntField(default=0)

    class Meta:
        table = "permission_overwrites"
        # One row per (target, subject) is enforced by partial unique indexes in the
        # migration: a plain unique key treats the NULL columns as all different.
