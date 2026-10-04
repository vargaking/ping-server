from tortoise import models, fields


class Channel(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)
    channel_settings = fields.JSONField(default=dict)
    type = fields.CharField(max_length=10, default="text")  # text or voice
    topic = fields.CharField(max_length=1024, null=True)
    position = fields.IntField(default=0)
    server = fields.ForeignKeyField(
        "models.Server", related_name="channels")
    group = fields.ForeignKeyField(
        "models.ChannelGroup", related_name="channels",
        null=True, on_delete=fields.SET_NULL)

    def __str__(self):
        return self.name

    class Meta:
        table = "channels"
        ordering = ["-created_at"]
