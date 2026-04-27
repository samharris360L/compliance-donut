#!/usr/bin/env python3
"""
Fetch certification compliance data from 360Learning and write data.json.

Uses the per-user certificates endpoint:
  GET /api/v2/users/{userId}/certificates

For each active user, we list their certificates and compare each one's
deliveryDate / expirationDate against "now". A user holds a specific
certificate (by ID) if they have at least one entry where:
    deliveryDate <= now < expirationDate

Compliance is BINARY and combined across all configured certificates:
    compliant      = holds every configured certificate (in-date)
    nonCompliant   = anything else

Non-compliance is broken out by reason:
    lapsedAtLeastOne   - has at least one expired version of a tracked
                         cert, but no in-date version
    missingAtLeastOne  - has never held at least one tracked cert

Config:
    - CERTIFICATES env var: JSON list, e.g.
        [{"id": "...", "label": "Safety"}, {"id": "...", "label": "GDPR"}]
      You can set this as a repo variable in GitHub Actions.
    - RENEWAL_WINDOWS env var (optional): comma-separated days, default "30,90"
    - Secrets: CLIENT_ID, CLIENT_SECRET
    - Optional: BASE_URL
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- CONFIG ------------------------------------------------------------------

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL", "https://app.360learning.com")

# CERTIFICATES is provided as JSON in an env var so it can be managed
# from GitHub Actions "Variables" without editing the script.
# Example value:
#   [{"id": "64a...", "label": "Safety Training"},
#    {"id": "64b...", "label": "GDPR Foundations"}]
_CERTS_RAW = os.environ.get("CERTIFICATES", "").strip()

# Renewal windows in days (comma-separated env var, default "30,90").
_WINDOWS_RAW = os.environ.get("RENEWAL_WINDOWS", "30,90").strip()

OUTPUT_PATH = Path(__file__).parent / "data.json"
API_VERSION = "v2.0"
MAX_PAGES = 50

# Progress log every N users processed, so the GitHub Actions log
# stays useful even on large platforms.
PROGRESS_EVERY = 100


# --- Config parsing ----------------------------------------------------------

def parse_certificates_env(raw):
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"CERTIFICATES env var is not valid JSON: {exc}")
    if not isinstance(parsed, list):
        raise SystemExit("CERTIFICATES env var must be a JSON array.")
    cleaned = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict) or "id" not in entry:
            raise SystemExit(f"CERTIFICATES entry #{i+1} must be an object with an 'id' field.")
        cleaned.append({
            "id": str(entry["id"]).strip(),
            "label": str(entry.get("label") or entry["id"]).strip(),
        })
    return cleaned


def parse_windows_env(raw):
    try:
        return sorted({int(x.strip()) for x in raw.split(",") if x.strip()})
    except ValueError:
        raise SystemExit(f"RENEWAL_WINDOWS must be comma-separated integers, got: {raw!r}")


CERTIFICATES = parse_certificates_env(_CERTS_RAW)
RENEWAL_WINDOWS = parse_windows_env(_WINDOWS_RAW) or [30, 90]


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


def paginated_get(url, headers, label=""):
    collected = []
    for page_num in range(1, MAX_PAGES + 1):
        t0 = time.monotonic()
        resp = _get_with_retry(url, headers)
        elapsed = time.monotonic() - t0
        body = resp.json()
        items = body.get("data", body.get("results", body)) if isinstance(body, dict) else body
        if isinstance(items, list):
            collected.extend(items)
            if label:
                print(f"    [{label}] page {page_num}: +{len(items)} ({elapsed:.1f}s, total {len(collected)})", flush=True)
        next_url = parse_next_link(resp.headers.get("link"))
        if not next_url:
            break
        url = next_url
    else:
        print(f"Note: page cap ({MAX_PAGES}) hit on {url}", file=sys.stderr, flush=True)
    return collected


def _get_with_retry(url, headers, max_attempts=4):
    """GET with retries on 429 (rate limit) and 5xx."""
    for attempt in range(1, max_attempts + 1):
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("retry-after") or (2 ** attempt))
            print(f"    rate limited, sleeping {wait}s (attempt {attempt}/{max_attempts})", flush=True)
            time.sleep(wait)
            continue
        if 500 <= resp.status_code < 600 and attempt < max_attempts:
            wait = 2 ** attempt
            print(f"    server {resp.status_code}, retrying in {wait}s (attempt {attempt}/{max_attempts})", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


# --- 360Learning fetch -------------------------------------------------------

def fetch_active_users(headers):
    users = paginated_get(f"{BASE_URL}/api/v2/users", headers, label="users")

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


def fetch_user_certificates(user_id, headers):
    """Return the raw list of certificates awarded to one user."""
    # Quiet label so we don't spam the log for every single user.
    return paginated_get(
        f"{BASE_URL}/api/v2/users/{user_id}/certificates", headers, label="",
    )


# --- Classification ----------------------------------------------------------

def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def certificate_id_of(cert):
    """
    A certificate record can reference its template / definition by any of
    several field names depending on the endpoint version. Be tolerant.
    """
    for key in ("certificateId", "templateId", "certificateTemplateId", "_id", "id"):
        v = cert.get(key)
        if v:
            return str(v)
    return None


def classify_user(user_certs, tracked_ids, now):
    """
    For each tracked certificate ID, decide the user's status:
        "compliant" - has at least one record that is in-date now
        "lapsed"    - has at least one record but none are in-date now
        "missing"   - has no records for this cert at all

    Returns (per_cert_status: dict, earliest_valid_expiry: datetime | None).
    """
    # Group this user's certificate records by their tracked ID.
    grouped = {cid: [] for cid in tracked_ids}
    for c in user_certs:
        cid = certificate_id_of(c)
        if cid in grouped:
            grouped[cid].append(c)

    per_cert_status = {}
    earliest_expiry = None

    for cid in tracked_ids:
        records = grouped.get(cid, [])
        if not records:
            per_cert_status[cid] = "missing"
            continue

        in_date = False
        for r in records:
            delivery = parse_iso(r.get("deliveryDate"))
            expiry = parse_iso(r.get("expirationDate"))
            # In-date = delivered on/before now AND expiring after now.
            # If the API returns no expiry, treat the cert as permanent.
            delivered_ok = delivery is None or delivery <= now
            expiry_ok = expiry is None or expiry > now
            if delivered_ok and expiry_ok:
                in_date = True
                if expiry is not None:
                    if earliest_expiry is None or expiry < earliest_expiry:
                        earliest_expiry = expiry

        per_cert_status[cid] = "compliant" if in_date else "lapsed"

    return per_cert_status, earliest_expiry


# --- Main --------------------------------------------------------------------

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: CLIENT_ID and CLIENT_SECRET must be set.", file=sys.stderr)
        return 1
    if not CERTIFICATES:
        print("ERROR: CERTIFICATES env var is empty. Set it as a GitHub Actions", file=sys.stderr)
        print('repository variable, e.g.:', file=sys.stderr)
        print('  [{"id":"64abc...","label":"Safety"},{"id":"64def...","label":"GDPR"}]', file=sys.stderr)
        return 1

    tracked_ids = [c["id"] for c in CERTIFICATES]
    labels = {c["id"]: c["label"] for c in CERTIFICATES}
    print(f"Tracking {len(tracked_ids)} certificate(s): {', '.join(labels.values())}", flush=True)
    print(f"Renewal windows: {RENEWAL_WINDOWS} days", flush=True)

    try:
        print(f"Getting token from {BASE_URL} ...", flush=True)
        token = get_access_token()
        headers = {
            "accept": "application/json",
            "360-api-version": API_VERSION,
            "authorization": f"Bearer {token}",
        }

        print("Fetching all active users ...", flush=True)
        active_users = fetch_active_users(headers)
        total_active = len(active_users)
        print(f"  {total_active} active users.", flush=True)

        now = datetime.now(timezone.utc)

        compliant_total = 0
        non_compliant_total = 0
        lapsed_any_total = 0
        missing_any_total = 0
        renewal_counts = {str(w): 0 for w in RENEWAL_WINDOWS}
        per_cert = {cid: {"compliant": 0, "lapsed": 0, "missing": 0} for cid in tracked_ids}

        print("Processing users ...", flush=True)
        t_start = time.monotonic()
        for i, u in enumerate(active_users, 1):
            uid = u.get("_id") or u.get("id")
            if not uid:
                continue
            try:
                user_certs = fetch_user_certificates(uid, headers)
            except requests.exceptions.HTTPError as exc:
                # A single user erroring shouldn't abort the whole run.
                print(f"    WARN: user {uid} errored ({exc}); treating as having no certs.", file=sys.stderr, flush=True)
                user_certs = []

            per_cert_status, earliest_expiry = classify_user(user_certs, tracked_ids, now)

            # Per-cert totals.
            for cid, status in per_cert_status.items():
                per_cert[cid][status] += 1

            # Overall (binary).
            statuses = list(per_cert_status.values())
            if all(s == "compliant" for s in statuses):
                compliant_total += 1
                if earliest_expiry is not None:
                    days_left = (earliest_expiry - now).days
                    for w in RENEWAL_WINDOWS:
                        if 0 <= days_left <= w:
                            renewal_counts[str(w)] += 1
            else:
                non_compliant_total += 1
                if "lapsed" in statuses:
                    lapsed_any_total += 1
                else:
                    missing_any_total += 1

            if i % PROGRESS_EVERY == 0 or i == total_active:
                rate = i / max(time.monotonic() - t_start, 0.01)
                remaining = (total_active - i) / max(rate, 0.01)
                print(f"  {i}/{total_active} users ({rate:.1f}/s, ~{remaining:.0f}s left)", flush=True)

        per_cert_list = []
        for cid in tracked_ids:
            per_cert_list.append({
                "id": cid,
                "label": labels[cid],
                **per_cert[cid],
            })

        summary = {
            "generatedAt": now.isoformat(),
            "renewalWindows": RENEWAL_WINDOWS,
            "certificateCount": len(tracked_ids),
            "totalActiveUsers": total_active,
            "compliant": compliant_total,
            "nonCompliant": non_compliant_total,
            "nonComplianceReasons": {
                "lapsedAtLeastOne": lapsed_any_total,
                "missingAtLeastOne": missing_any_total,
            },
            "renewalsDue": renewal_counts,
            "certificates": per_cert_list,
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
