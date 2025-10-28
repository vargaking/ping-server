from tortoise import models, fields


class RoleToUser(models.Model):
    role = fields.ForeignKeyField("models.Role", related_name="role_users")
    user = fields.ForeignKeyField("models.User", related_name="user_roles")
    assigned_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "role_to_user"
        unique_together = (("role", "user"),)
