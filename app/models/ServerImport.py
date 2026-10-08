import uuid

from tortoise import fields, models


class ServerImport(models.Model):
    """An export bundle uploaded to a server, and how its import is going.

    The files live in IMPORTS_ROOT/<id>."""
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    server = fields.ForeignKeyField(
        "models.Server", related_name="imports", on_delete=fields.CASCADE)
    created_by = fields.ForeignKeyField(
        "models.User", related_name="server_imports", null=True, on_delete=fields.SET_NULL)
    # uploading | unpacking | ready | importing | done | failed
    status = fields.CharField(max_length=12)
    filename = fields.CharField(max_length=255)
    size = fields.BigIntField()
    received = fields.BigIntField(default=0)
    source = fields.CharField(max_length=200, null=True)
    source_platform = fields.CharField(max_length=50, null=True)
    source_name = fields.CharField(max_length=200, null=True)
    # Source author id to user id.
    authors = fields.JSONField(default=dict)
    plan = fields.JSONField(null=True)
    result = fields.JSONField(null=True)
    progress = fields.JSONField(null=True)
    # unpacking | importing
    failed_step = fields.CharField(max_length=12, null=True)
    error = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "server_imports"
