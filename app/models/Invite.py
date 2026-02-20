import uuid

import bcrypt
from tortoise import fields, models


class Invite(models.Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    server = fields.ForeignKeyField("models.Server", related_name="invites")
    created_by = fields.ForeignKeyField("models.User", related_name="created_invites")
    created_at = fields.DatetimeField(auto_now_add=True)
    valid_until = fields.DatetimeField(null=True)
    max_uses = fields.IntField(null=True)
    use_count = fields.IntField(default=0)
    password_hash = fields.TextField(null=True)
    is_active = fields.BooleanField(default=True)

    def set_password(self, password: str):
        salt = bcrypt.gensalt()
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), salt
        ).decode("utf-8")

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return True
        return bcrypt.checkpw(
            password.encode("utf-8"), self.password_hash.encode("utf-8")
        )

    def __str__(self):
        return f"Invite {self.id} for server {self.server_id}"

    class Meta:
        table = "invites"
        ordering = ["-created_at"]
