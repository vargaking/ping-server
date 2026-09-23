from tortoise import models, fields


class Message(models.Model):
    id = fields.IntField(pk=True, generated=True)
    uuid = fields.UUIDField(unique=True)
    content = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)
    author = fields.ForeignKeyField("models.User", related_name="messages")
    # A message belongs to either a server channel or a DM conversation, never
    # both. server/channel are set for channel messages; conversation is set for
    # direct messages. All three are nullable so one row shape serves both.
    server = fields.ForeignKeyField(
        "models.Server", related_name="messages", null=True)
    channel = fields.ForeignKeyField(
        "models.Channel", related_name="messages", null=True)
    conversation = fields.ForeignKeyField(
        "models.Conversation", related_name="messages", null=True)
    timestamp = fields.DatetimeField()
    edited_at = fields.DatetimeField(null=True)
    metadata = fields.JSONField(default=dict)

    def __str__(self):
        where = f"channel {self.channel_id}" if self.channel_id else f"conversation {self.conversation_id}"
        return f"Message {self.uuid} in {where}"

    class Meta:
        table = "messages"
        ordering = ["-created_at"]
