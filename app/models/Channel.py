from tortoise import models, fields


class Channel(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)
    channel_settings = fields.JSONField(default={})
    type = fields.CharField(max_length=10, default="text") # text or voice

    def __str__(self):
        return self.name

    class Meta:
        table = "channels"
        ordering = ["-created_at"]