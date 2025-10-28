from tortoise import models, fields


class ChannelToServer(models.Model):
    channel = fields.ForeignKeyField("models.Channel", related_name="channel_servers")
    server = fields.ForeignKeyField("models.Server", related_name="server_channels")
    created_at = fields.DatetimeField(auto_now_add=True)