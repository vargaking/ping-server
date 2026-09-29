from tortoise import fields, models


class Attachment(models.Model):
    """An uploaded file that belongs to a channel or DM conversation.

    A row is created by the upload and linked to its message when the message
    is sent; rows that never get a message are pruned. The file itself lives
    outside the public media mount and is only served through an authenticated
    route (see routers/attachments).
    """
    id = fields.UUIDField(primary_key=True)
    uploader = fields.ForeignKeyField(
        "models.User", related_name="attachments", on_delete=fields.CASCADE)
    server = fields.ForeignKeyField(
        "models.Server", null=True, on_delete=fields.CASCADE)
    channel = fields.ForeignKeyField(
        "models.Channel", null=True, on_delete=fields.CASCADE)
    conversation = fields.ForeignKeyField(
        "models.Conversation", null=True, on_delete=fields.CASCADE)
    message = fields.ForeignKeyField(
        "models.Message", null=True, related_name="attachments",
        on_delete=fields.CASCADE)
    filename = fields.CharField(max_length=255)
    content_type = fields.CharField(max_length=255)
    size = fields.IntField()
    # "image" only for bytes that Pillow verified; everything else is "file".
    kind = fields.CharField(max_length=10)
    width = fields.IntField(null=True)
    height = fields.IntField(null=True)
    # Relative to ATTACHMENTS_ROOT.
    storage_path = fields.CharField(max_length=512)
    created_at = fields.DatetimeField(auto_now_add=True)

    def to_json(self) -> dict:
        return {
            "id": str(self.id),
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "kind": self.kind,
            "width": self.width,
            "height": self.height,
            "url": f"/attachments/{self.id}",
        }

    class Meta:
        table = "attachments"
