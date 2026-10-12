from tortoise import fields, models


class AnnouncementDismissal(models.Model):
    """A user having dismissed an announcement."""
    id = fields.IntField(pk=True)
    announcement = fields.ForeignKeyField(
        "models.Announcement", related_name="dismissals", on_delete=fields.CASCADE)
    user = fields.ForeignKeyField(
        "models.User", related_name="announcement_dismissals", on_delete=fields.CASCADE)

    class Meta:
        table = "announcement_dismissals"
        unique_together = (("announcement", "user"),)
        indexes = (("user",),)
