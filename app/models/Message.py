from tortoise import models, fields


class Message(models.Model):
    id = fields.IntField(pk=True, generated=True)
    uuid = fields.UUIDField(unique=True)
    content = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)
    author = fields.ForeignKeyField("models.User", related_name="messages")
    server = fields.ForeignKeyField("models.Server", related_name="messages")
    channel = fields.ForeignKeyField("models.Channel", related_name="messages")
    timestamp = fields.DatetimeField()
    metadata = fields.JSONField(default=dict)

    def __str__(self):
        return f"Message by {self.author.username} in {self.channel.name}"

    class Meta:
        table = "messages"
        ordering = ["-created_at"]
