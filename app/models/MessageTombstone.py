from tortoise import fields, models


class MessageTombstone(models.Model):
    """A deleted message's id, kept for the changes feed until it is pruned."""
    id = fields.IntField(pk=True)
    message_uuid = fields.UUIDField()
    server_id = fields.IntField(null=True)
    channel = fields.ForeignKeyField(
        "models.Channel", related_name="tombstones", null=True,
        on_delete=fields.CASCADE)
    conversation = fields.ForeignKeyField(
        "models.Conversation", related_name="tombstones", null=True,
        on_delete=fields.CASCADE)
    deleted_at = fields.DatetimeField()

    class Meta:
        table = "message_tombstones"
        indexes = (("deleted_at",),)
