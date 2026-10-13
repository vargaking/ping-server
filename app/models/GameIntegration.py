from tortoise import fields, models


class GameIntegration(models.Model):
    """A game server allowed to drive the voice mix in one voice channel."""
    id = fields.IntField(pk=True)
    server = fields.ForeignKeyField(
        "models.Server", related_name="game_integrations", on_delete=fields.CASCADE)
    name = fields.CharField(max_length=32)
    voice_channel = fields.ForeignKeyField(
        "models.Channel", related_name="game_integrations",
        null=True, on_delete=fields.SET_NULL)
    players_hear_spectators = fields.BooleanField(default=False)
    key_hash = fields.CharField(max_length=64, unique=True)
    key_prefix = fields.CharField(max_length=12)
    created_by = fields.ForeignKeyField(
        "models.User", related_name="created_game_integrations",
        null=True, on_delete=fields.SET_NULL)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "game_integrations"
        indexes = (("server",),)
