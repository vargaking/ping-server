from tortoise import fields, models


class ReadState(models.Model):
    """How far a user has read into a channel or a DM conversation.

    Exactly one of channel/conversation is set, mirroring Message. The marker
    is a plain int (last_read_message_id), not a foreign key to Message: the
    marker message may later be deleted, and losing the read position when
    that happens would be worse than pointing at a row that no longer exists.
    """
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField(
        "models.User", related_name="read_states", on_delete=fields.CASCADE)
    channel = fields.ForeignKeyField(
        "models.Channel", related_name="read_states", null=True,
        on_delete=fields.CASCADE)
    conversation = fields.ForeignKeyField(
        "models.Conversation", related_name="read_states", null=True,
        on_delete=fields.CASCADE)
    last_read_message_id = fields.IntField()
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "read_states"
        unique_together = (("user", "channel"), ("user", "conversation"))
