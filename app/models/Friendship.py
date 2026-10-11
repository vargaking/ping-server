from enum import StrEnum

from tortoise import fields, models


class FriendshipStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"


class Friendship(models.Model):
    """A friend request or an accepted friendship between two users.

    One row per unordered pair, stored low-high like ``Conversation``: always go
    through ``normalize_pair``. ``requester`` is whichever of the two asked.
    """
    id = fields.IntField(pk=True)
    user_a = fields.ForeignKeyField(
        "models.User", related_name="friendships_as_a",
        on_delete=fields.CASCADE)
    user_b = fields.ForeignKeyField(
        "models.User", related_name="friendships_as_b",
        on_delete=fields.CASCADE)
    requester = fields.ForeignKeyField(
        "models.User", related_name="friend_requests_sent",
        on_delete=fields.CASCADE)
    status = fields.CharField(max_length=10, default=FriendshipStatus.PENDING.value)
    created_at = fields.DatetimeField(auto_now_add=True)
    accepted_at = fields.DatetimeField(null=True)

    @staticmethod
    def normalize_pair(first_id: int, second_id: int) -> tuple[int, int]:
        """Return (user_a_id, user_b_id) in the canonical low-high order."""
        return (first_id, second_id) if first_id < second_id else (second_id, first_id)

    def other_user_id(self, user_id: int) -> int:
        """The participant that isn't *user_id*."""
        return self.user_b_id if user_id == self.user_a_id else self.user_a_id

    class Meta:
        table = "friendships"
        unique_together = (("user_a", "user_b"),)
        indexes = (("user_b",), ("requester",))
