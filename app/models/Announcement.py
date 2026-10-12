from enum import StrEnum

from tortoise import fields, models


class AnnouncementLevel(StrEnum):
    INFO = "info"
    MAINTENANCE = "maintenance"
    INCIDENT = "incident"


class Announcement(models.Model):
    """A platform-wide notice posted by a platform admin."""
    id = fields.IntField(pk=True)
    text = fields.CharField(max_length=500)
    level = fields.CharField(max_length=16)
    created_by = fields.ForeignKeyField(
        "models.User", related_name="created_announcements",
        null=True, on_delete=fields.SET_NULL)
    created_at = fields.DatetimeField(auto_now_add=True)
    expires_at = fields.DatetimeField(null=True)

    class Meta:
        table = "announcements"
