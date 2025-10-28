from tortoise import fields, models
import bcrypt


class User(models.Model):
    id = fields.IntField(pk=True)
    username = fields.CharField(max_length=50, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    is_active = fields.BooleanField(default=True)
    public_key = fields.TextField(null=True)
    profile = fields.JSONField(default={})
    password_hash = fields.TextField()

    def set_password(self, password: str):
        salt = bcrypt.gensalt()
        self.password_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

    def check_password(self, password: str) -> bool:
        return bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8'))

    @classmethod
    async def create_with_password(cls, password: str, **kwargs):
        user = cls(**kwargs)
        user.set_password(password)
        await user.save()
        return user

    def __str__(self):
        return self.username

    class Meta:
        table = "users"
        ordering = ["-created_at"]
