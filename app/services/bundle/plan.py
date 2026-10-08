"""The JSON form of an import report, as the settings import shows it."""
from dataclasses import asdict

from .importer import ChannelReport, Report

_CHANNEL_FIELDS = (
    "source_id", "name", "type", "action", "target_name", "reason", "category", "messages",
    "existing_messages", "posts", "existing_posts", "attachments", "attachment_bytes",
    "handed_over")


def _channel(report: ChannelReport) -> dict:
    return {name: getattr(report, name) for name in _CHANNEL_FIELDS}


def plan_json(report: Report) -> dict:
    left_out = asdict(report.left_out)
    left_out.update(
        over_attachment_limit=report.over_attachment_limit,
        tags_over_limit=report.tags_over_limit,
        empty_messages=report.empty_skipped,
        invalid_messages=report.invalid_skipped,
    )
    return {
        "channels": [_channel(c) for c in report.channels],
        "totals": {
            "messages": report.messages,
            "existing_messages": sum(c.existing_messages for c in report.channels),
            "posts": report.posts,
            "attachments": report.attachments,
            "attachment_bytes": report.attachment_bytes,
        },
        "free_bytes": report.free_bytes,
        "over_cap": report.over_cap,
        "missing": report.missing,
        "left_out": left_out,
        "warnings": list(report.warnings),
    }
