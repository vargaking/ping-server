from tortoise import models, fields


class MessageToChannel(models.Model):
    message = fields.ForeignKeyField("models.Message", related_name="message_channels")
    channel = fields.ForeignKeyField("models.Channel", related_name="channel_messages")
    created_at = fields.DatetimeField(auto_now_add=True)