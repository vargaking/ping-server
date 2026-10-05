from tortoise import fields, models


class ForumPostTag(models.Model):
    id = fields.IntField(pk=True)
    post = fields.ForeignKeyField(
        "models.ForumPost", related_name="post_tags", on_delete=fields.CASCADE)
    tag = fields.ForeignKeyField(
        "models.ForumTag", related_name="post_tags", on_delete=fields.CASCADE)

    class Meta:
        table = "forum_post_tags"
        unique_together = (("post", "tag"),)
