from tortoise import fields, models


class Reaction(models.Model):
    """One user's emoji reaction to a message."""
    id = fields.IntField(pk=True, generated=True)
    message = fields.ForeignKeyField(
        "models.Message", related_name="reactions", on_delete=fields.CASCADE)
    user = fields.ForeignKeyField(
        "models.User", related_name="reactions", on_delete=fields.CASCADE)
    emoji = fields.CharField(max_length=64)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "reactions"
        unique_together = (("message", "user", "emoji"),)
        indexes = (("message",),)
