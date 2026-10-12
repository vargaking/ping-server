from enum import StrEnum

from tortoise import fields, models


class CallState(StrEnum):
    RINGING = "ringing"
    ACTIVE = "active"
    ENDED = "ended"
    MISSED = "missed"
    DECLINED = "declined"


class CallEndReason(StrEnum):
    HANGUP = "hangup"
    ALONE = "alone"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    DECLINED = "declined"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


class Call(models.Model):
    """A call in a DM conversation, shown in the thread as its message.

    open_key holds the conversation id while the call rings or is active and is
    NULL once it ends, so its unique constraint allows one open call per conversation.
    """
    id = fields.IntField(pk=True)
    conversation = fields.ForeignKeyField(
        "models.Conversation", related_name="calls", on_delete=fields.CASCADE)
    caller = fields.ForeignKeyField(
        "models.User", related_name="calls_started", on_delete=fields.CASCADE)
    callee = fields.ForeignKeyField(
        "models.User", related_name="calls_received", on_delete=fields.CASCADE)
    message = fields.OneToOneField(
        "models.Message", related_name="call_record", on_delete=fields.CASCADE)
    state = fields.CharField(max_length=10)
    reason = fields.CharField(max_length=12, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    ring_expires_at = fields.DatetimeField()
    answered_at = fields.DatetimeField(null=True)
    ended_at = fields.DatetimeField(null=True)
    open_key = fields.IntField(null=True, unique=True)

    class Meta:
        table = "calls"
        indexes = (("conversation",), ("caller",), ("callee",))
