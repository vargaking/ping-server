from tortoise import models, fields


class ChannelGroup(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    position = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    server = fields.ForeignKeyField(
        "models.Server", related_name="channel_groups")

    def __str__(self):
        return self.name

    class Meta:
        table = "channel_groups"
        ordering = ["position", "id"]
