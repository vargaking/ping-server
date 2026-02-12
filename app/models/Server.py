from tortoise import models, fields


class Server(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)
    server_profile = fields.JSONField(default=dict)
    server_settings = fields.JSONField(default=dict)

    def __str__(self):
        return self.name

    class Meta:
        table = "servers"
        ordering = ["-created_at"]
