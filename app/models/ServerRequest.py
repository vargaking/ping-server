from tortoise import fields, models


class ServerRequest(models.Model):
    """A user's request to create a server while creation is gated."""
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField(
        "models.User", related_name="server_requests", on_delete=fields.CASCADE)
    name = fields.CharField(max_length=100)
    description = fields.TextField()
    # lt10 | 10to50 | 50plus
    expected_size = fields.CharField(max_length=8)
    # pending | approved | declined | withdrawn
    status = fields.CharField(max_length=10, default="pending", db_index=True)
    decline_reason = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    decided_at = fields.DatetimeField(null=True)
    decided_by = fields.ForeignKeyField(
        "models.User", related_name="decided_server_requests",
        null=True, on_delete=fields.SET_NULL)
    server = fields.ForeignKeyField(
        "models.Server", related_name="creation_request",
        null=True, on_delete=fields.SET_NULL)

    class Meta:
        table = "server_requests"
        ordering = ["-created_at"]
