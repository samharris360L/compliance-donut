#!/usr/bin/env python3
"""
Fetch certification compliance data from 360Learning and write data.json.

Compliance is BINARY and combined across all configured paths.

For each active user, we look at every configured path and find their
latest completion. A user is compliant only if they have a valid,
in-date completion on EVERY configured path. If any single path is
missing or lapsed, the user is non-compliant overall.

We also break down non-compliance by reason (lapsed at least one path
vs. never completed at least one path), and count compliant users
whose next expiry falls within 30 or 90 days.

Config via constants at the top. Secrets via env vars.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# --- CONFIG ------------------------------------------------------------------

# Add as many path IDs as you like. A user must be compliant on EVERY
# path to count as compliant overall.
PATHS = [
    # {"id": "507f1f77bcf86cd799439011", "label": "Safety Training"},
    # {"id": "abcdef1234567890abcdef12", "label": "GDPR Foundations"},
]

# How long is a certificate valid for (months)?
VALIDITY_MONTHS = 12

# "Renewing soon" windows, in days.
RENEWAL_WINDOWS = [30, 90]

# -----------------------------------------------------------------------------

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL", "https://app.360learning.com")

OUTPUT_PATH = Path(__file__).parent / "data.json"
API_VERSION = "v2.0"
MAX_PAGES = 50


# --- HTTP helpers ------------------------------------------------------------

def get_access_token():
    resp = requests.post(
        f"{BASE_URL}/api/v2/oauth2/token",
        headers={"accept": "application/json", "content-type": "application/json"},
        json={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def parse_next_link(link_header):
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>\s*;\s*rel="next"', link_header)
    return match.group(1) if match else None


def paginated_get(url, headers):
    collected = []
    for _ in range(MAX_PAGES):
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        items = body.get("data", body.get("results", body)) if isinstance(body, dict) else body
        if isinstance(items, list):
            collected.extend(items)
        next_url = parse_next_link(resp.headers.get("link"))
        if not next_url:
            break
        url = next_url
    else:
        print(f"Note: page cap ({MAX_PAGES}) hit on {url}", file=sys.stderr)
    return collected


# --- 360Learning data fetch --------------------------------------------------

def fetch_active_users(headers):
    users = paginated_get(f"{BASE_URL}/api/v2/users", headers)

    def is_active(u):
        if u.get("deleted") is True:
            return False
        status = (u.get("status") or "").lower()
        if status in ("deactivated", "deleted", "disabled", "archived"):
            return False
        if u.get("active") is False:
            return False
        return True

    return [u for u in users if is_active(u)]


def fetch_latest_completions_for_path(path_id, headers):
    """
    Return a dict: userId -> latest completion datetime (UTC) across
    every session of the given path.
    """
    sessions = paginated_get(f"{BASE_URL}/api/v2/paths/{path_id}/sessions", headers)
    print(f"  path {path_id}: {len(sessions)} sessions")

    latest = {}
    for s in sessions:
        session_id = s.get("_id") or s.get("id")
        if not session_id:
            continue
        stats = paginated_get(
            f"{BASE_URL}/api/v2/paths/{path_id}/sessions/{session_id}/user-stats",
            headers,
        )
        for row in stats:
            uid = row.get("userId")
            completed = parse_iso(row.get("completedAt"))
            if not uid or not completed:
                continue
            prev = latest.get(uid)
            if prev is None or completed > prev:
                latest[uid] = completed
    return latest


# --- Helpers -----------------------------------------------------------------

def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# --- Core logic --------------------------------------------------------------

def classify_users(active_users, completions_by_path, now, validity):
    """
    completions_by_path: { path_id: { user_id: datetime } }

    For each user, build a per-path status:
        - "compliant" if latest completion + validity >= now
        - "lapsed"    if latest completion + validity < now
        - "missing"   if no completion on record

    Overall status is BINARY:
        - "compliant"      : compliant on ALL configured paths
        - "nonCompliant"   : anything else

    Return totals, a breakdown of non-compliance reasons, and an
    "earliest expiry" per compliant user for renewal counting.
    """
    total_active = len(active_users)
    compliant = 0
    non_compliant = 0

    # Non-compliance breakdown (a user counts in at most one bucket).
    lapsed_any = 0     # at least one path is lapsed
    missing_any = 0    # no lapses, but at least one path never completed

    # Renewal buckets — only for fully-compliant users.
    renewal_counts = {str(w): 0 for w in RENEWAL_WINDOWS}

    # Per-path stats (useful to surface, even though compliance is
    # combined). For each path: how many active users are compliant /
    # lapsed / missing for just that path.
    per_path_stats = {
        pid: {"compliant": 0, "lapsed": 0, "missing": 0}
        for pid in completions_by_path
    }

    for u in active_users:
        uid = u.get("_id") or u.get("id")
        if not uid:
            continue

        statuses = []
        earliest_expiry = None

        for pid, comp_map in completions_by_path.items():
            completed = comp_map.get(uid)
            if completed is None:
                statuses.append("missing")
                per_path_stats[pid]["missing"] += 1
                continue
            expiry = completed + validity
            if expiry >= now:
                statuses.append("compliant")
                per_path_stats[pid]["compliant"] += 1
                if earliest_expiry is None or expiry < earliest_expiry:
                    earliest_expiry = expiry
            else:
                statuses.append("lapsed")
                per_path_stats[pid]["lapsed"] += 1

        if all(s == "compliant" for s in statuses):
            compliant += 1
            if earliest_expiry is not None:
                days_left = (earliest_expiry - now).days
                for w in RENEWAL_WINDOWS:
                    if 0 <= days_left <= w:
                        renewal_counts[str(w)] += 1
        else:
            non_compliant += 1
            if "lapsed" in statuses:
                lapsed_any += 1
            else:
                missing_any += 1

    return {
        "totalActiveUsers": total_active,
        "compliant": compliant,
        "nonCompliant": non_compliant,
        "nonComplianceReasons": {
            "lapsedAtLeastOne": lapsed_any,
            "missingAtLeastOne": missing_any,
        },
        "renewalsDue": renewal_counts,
        "perPath": per_path_stats,
    }


# --- Main --------------------------------------------------------------------

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: CLIENT_ID and CLIENT_SECRET must be set.", file=sys.stderr)
        return 1
    if not PATHS:
        print("ERROR: Add at least one entry to PATHS at the top of fetch_data.py.", file=sys.stderr)
        return 1

    try:
        print(f"Getting token from {BASE_URL} ...")
        token = get_access_token()
        headers = {
            "accept": "application/json",
            "360-api-version": API_VERSION,
            "authorization": f"Bearer {token}",
        }

        print("Fetching all active users ...")
        active_users = fetch_active_users(headers)
        print(f"  {len(active_users)} active users.")

        completions_by_path = {}
        path_labels = {}
        for p in PATHS:
            print(f"Processing path '{p['label']}' ({p['id']}) ...")
            completions_by_path[p["id"]] = fetch_latest_completions_for_path(p["id"], headers)
            path_labels[p["id"]] = p["label"]

        now = datetime.now(timezone.utc)
        validity = timedelta(days=VALIDITY_MONTHS * 30)  # approximate month

        result = classify_users(active_users, completions_by_path, now, validity)

        # Attach labels to the per-path section so the widget doesn't need
        # to cross-reference PATHS separately.
        per_path_list = []
        for p in PATHS:
            stats = result["perPath"].get(p["id"], {})
            per_path_list.append({
                "id": p["id"],
                "label": p["label"],
                **stats,
            })

        summary = {
            "generatedAt": now.isoformat(),
            "validityMonths": VALIDITY_MONTHS,
            "renewalWindows": RENEWAL_WINDOWS,
            "pathCount": len(PATHS),
            "totalActiveUsers": result["totalActiveUsers"],
            "compliant": result["compliant"],
            "nonCompliant": result["nonCompliant"],
            "nonComplianceReasons": result["nonComplianceReasons"],
            "renewalsDue": result["renewalsDue"],
            "paths": per_path_list,
        }

    except requests.exceptions.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        print(f"Response body: {exc.response.text}", file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    OUTPUT_PATH.write_text(json.dumps(summary, indent=2))
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
