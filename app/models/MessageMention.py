from tortoise import fields, models


class MessageMention(models.Model):
    """A user mentioned by a channel message. DMs get no rows."""
    id = fields.IntField(pk=True)
    message = fields.ForeignKeyField(
        "models.Message", related_name="mentions", on_delete=fields.CASCADE)
    user = fields.ForeignKeyField(
        "models.User", related_name="mentions_received", on_delete=fields.CASCADE)
    channel = fields.ForeignKeyField(
        "models.Channel", related_name="mentions", on_delete=fields.CASCADE)

    class Meta:
        table = "message_mentions"
        unique_together = (("message", "user"),)
        indexes = (("user", "channel", "message"),)
