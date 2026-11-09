"""Markers for read-only session rows that mirror work outside Omnigent."""

from __future__ import annotations

from collections.abc import Mapping

EXTERNAL_KIND_LABEL_KEY = "omnigent.external.kind"
EXTERNAL_UNIT_LABEL_KEY = "omnigent.external.unit"
EXTERNAL_HOST_LABEL_KEY = "omnigent.external.host_id"
EXTERNAL_STATE_LABEL_KEY = "omnigent.external.state"
EXTERNAL_EXIT_CODE_LABEL_KEY = "omnigent.external.exit_code"
EXTERNAL_DETAIL_LABEL_KEY = "omnigent.external.detail"
EXTERNAL_KIND_SYSTEMD_UNIT = "systemd_unit"

# External state -> session live status shown by the sidebar.
EXTERNAL_STATE_TO_LIVE_STATUS: dict[str, str] = {
    "running": "running",
    "pending": "waiting",
    "waiting": "waiting",
    "idle": "idle",
    "completed": "idle",
    "succeeded": "idle",
    "failed": "failed",
}


def is_external_task(labels: Mapping[str, str] | None) -> bool:
    """Return whether session labels mark a read-only external task row."""
    return bool((labels or {}).get(EXTERNAL_KIND_LABEL_KEY))
