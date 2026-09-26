"""Exact admitted historical review provenance; no placement or current-work inference."""

RECORDS = {}


def reviews(project):
    return {
        "project": project,
        "reviews": [],
        "notice": "No admitted independent review records in this projection.",
    }
