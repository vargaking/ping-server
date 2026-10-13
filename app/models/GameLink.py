from tortoise import fields, models


class GameLink(models.Model):
    """A Zet user's account in one game integration."""
    id = fields.IntField(pk=True)
    integration = fields.ForeignKeyField(
        "models.GameIntegration", related_name="links", on_delete=fields.CASCADE)
    user = fields.ForeignKeyField(
        "models.User", related_name="game_links", on_delete=fields.CASCADE)
    player_id = fields.CharField(max_length=64)
    player_name = fields.CharField(max_length=64)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "game_links"
        unique_together = (("integration", "player_id"), ("integration", "user"))
