from tortoise import fields, models


class ForumTag(models.Model):
    id = fields.IntField(pk=True)
    channel = fields.ForeignKeyField(
        "models.Channel", related_name="forum_tags", on_delete=fields.CASCADE)
    name = fields.CharField(max_length=30)
    color = fields.CharField(max_length=7, null=True)
    position = fields.IntField(default=0)

    def __str__(self):
        return self.name

    class Meta:
        table = "forum_tags"
        unique_together = (("channel", "name"),)
        ordering = ["position", "id"]
