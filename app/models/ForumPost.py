from tortoise import fields, models


class ForumPost(models.Model):
    id = fields.IntField(pk=True)
    channel = fields.ForeignKeyField(
        "models.Channel", related_name="forum_posts", on_delete=fields.CASCADE)
    # Posts outlive their author's account.
    author = fields.ForeignKeyField(
        "models.User", related_name="forum_posts", null=True,
        on_delete=fields.SET_NULL)
    title = fields.CharField(max_length=200)
    pinned = fields.BooleanField(default=False)
    locked = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    # Server time of the last reply, never the client's message timestamp.
    last_activity_at = fields.DatetimeField()
    reply_count = fields.IntField(default=0)
    metadata = fields.JSONField(default=dict)
    # Stored rather than derived as the earliest message: timestamps are
    # client-supplied, so a reply from a skewed clock could sort before it.
    # A plain id, not a foreign key: messages and posts reference each other
    # and Tortoise can't generate that schema. It dangles if the opener's
    # account is deleted, which readers treat as "no opening message".
    opening_message_id = fields.IntField(null=True, unique=True)

    def __str__(self):
        return self.title

    class Meta:
        table = "forum_posts"
        indexes = (("channel", "last_activity_at"),)
