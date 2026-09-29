from tortoise import fields, models


class PushSubscription(models.Model):
    """A browser's Web Push subscription; a user can have several."""
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField(
        "models.User", related_name="push_subscriptions", on_delete=fields.CASCADE)
    endpoint = fields.CharField(max_length=2048, unique=True)
    p256dh = fields.CharField(max_length=128)
    auth = fields.CharField(max_length=64)
    created_at = fields.DatetimeField(auto_now_add=True)
    # Set on every successful send; drives which subscription is dropped first
    # when a user goes over the cap.
    last_used_at = fields.DatetimeField(null=True)

    class Meta:
        table = "push_subscriptions"
