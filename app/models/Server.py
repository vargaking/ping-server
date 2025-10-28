from tortoise import models, fields


class Server(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)
    server_profile = fields.JSONField(default={})
    server_settings = fields.JSONField(default={})
