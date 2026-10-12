from tortoise import fields, models


class ServerImportAuthorChange(models.Model):
    """One change to an import's author mapping, kept after the import row is gone.

    import_id is not a foreign key for that reason. A NULL from_user or to_user
    means the author was not mapped.
    """
    id = fields.IntField(pk=True)
    server = fields.ForeignKeyField(
        "models.Server", related_name="import_author_changes", on_delete=fields.CASCADE)
    import_id = fields.UUIDField(null=True)
    author_id = fields.CharField(max_length=255)
    author_name = fields.CharField(max_length=200)
    actor = fields.ForeignKeyField(
        "models.User", related_name="import_author_changes_made",
        null=True, on_delete=fields.SET_NULL)
    from_user = fields.ForeignKeyField(
        "models.User", related_name="import_author_changes_from",
        null=True, on_delete=fields.SET_NULL)
    to_user = fields.ForeignKeyField(
        "models.User", related_name="import_author_changes_to",
        null=True, on_delete=fields.SET_NULL)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "server_import_author_changes"
        indexes = (("server", "created_at"),)
