from tortoise import fields, models


class StatSample(models.Model):
    """One aggregate reading of a metric for a minute or an hour.

    Minute rows have avg == max. Scope is "platform" for the admin dashboard
    and leaves room for per-server series later.
    """
    id = fields.BigIntField(primary_key=True)
    scope = fields.CharField(max_length=32, default="platform")
    metric = fields.CharField(max_length=32)
    # minute | hour
    resolution = fields.CharField(max_length=8)
    # UTC, floored to the resolution
    bucket = fields.DatetimeField()
    avg = fields.FloatField()
    max = fields.FloatField()

    class Meta:
        table = "stat_samples"
        unique_together = (("scope", "metric", "resolution", "bucket"),)
        indexes = (("scope", "resolution", "bucket"),)
