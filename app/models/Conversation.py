from tortoise import fields, models


class Conversation(models.Model):
    """A 1:1 direct-message thread between two users.

    The pair is stored in a canonical order (user_a_id < user_b_id) so the
    unordered {A, B} pair has exactly one row. Get-or-create and the uniqueness
    constraint both rely on that ordering — always go through
    ``normalize_pair`` before looking one up or creating it.
    """
    id = fields.IntField(pk=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    user_a = fields.ForeignKeyField(
        "models.User", related_name="conversations_as_a",
        on_delete=fields.CASCADE)
    user_b = fields.ForeignKeyField(
        "models.User", related_name="conversations_as_b",
        on_delete=fields.CASCADE)

    @staticmethod
    def normalize_pair(first_id: int, second_id: int) -> tuple[int, int]:
        """Return (user_a_id, user_b_id) in the canonical low-high order."""
        return (first_id, second_id) if first_id < second_id else (second_id, first_id)

    def other_user_id(self, user_id: int) -> int:
        """The participant that isn't *user_id*."""
        return self.user_b_id if user_id == self.user_a_id else self.user_a_id

    def has_participant(self, user_id: int) -> bool:
        return user_id in (self.user_a_id, self.user_b_id)

    class Meta:
        table = "conversations"
        unique_together = (("user_a", "user_b"),)
        ordering = ["-created_at"]
