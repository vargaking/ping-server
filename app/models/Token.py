from tortoise import models, fields


class Token(models.Model):
    id = fields.IntField(pk=True, generated=True)
    user = fields.ForeignKeyField("models.User", related_name="tokens")
    token = fields.CharField(max_length=255, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    # When the session token stops being valid. Nullable: pre-existing tokens
    # (issued before this column) have no expiry and are treated as non-expiring
    # until they are rotated on the user's next login.
    expires_at = fields.DatetimeField(null=True)

    def __str__(self):
        return f"Token for {self.user.username}"

    class Meta:
        table = "tokens"
        ordering = ["-created_at"]
