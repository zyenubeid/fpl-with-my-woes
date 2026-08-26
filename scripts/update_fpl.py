#!/usr/bin/env python3
"""
Rebuild data.json for the "With my woes" FPL league webapp.
 
Exits 0 and writes nothing when no new gameweek has finished.
Only ever touches data.json.
"""
 
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
 
API = "https://fantasy.premierleague.com/api"
LEAGUE_ID = 1741238
LEAGUE_NAME = "With my woes"
SEASON = "2026/27"
DATA_FILE = "data.json"
 
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; fpl-with-my-woes-updater/1.0)",
    "Accept": "application/json",
}
 
 
def fetch(path, attempts=4):
    """GET a JSON endpoint with retries and exponential backoff."""
    url = API + path
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    raise RuntimeError("HTTP %s" % resp.status)
                return json.loads(resp.read().decode("utf-8"))
        except Exception as err:  # noqa: BLE001 - retry on anything transient
            last_err = err
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise RuntimeError("failed to fetch %s: %s" % (url, last_err))
 
 
def latest_finished_gw(bootstrap):
    """Highest gameweek that is BOTH finished and data_checked (bonus confirmed)."""
    done = [
        e["id"]
        for e in bootstrap.get("events", [])
        if e.get("finished") and e.get("data_checked")
    ]
    return max(done) if done else 0
 
 
def fetch_managers():
    """All managers in the league, following pagination."""
    managers = []
    page = 1
    while True:
        data = fetch(
            "/leagues-classic/%d/standings/?page_standings=%d" % (LEAGUE_ID, page)
        )
        standings = data.get("standings", {})
        for row in standings.get("results", []):
            managers.append(
                {
                    "entry": row["entry"],
                    "name": row["player_name"],
                    "team": row["entry_name"],
                }
            )
        if not standings.get("has_next"):
            break
        page += 1
        if page > 50:  # safety valve
            raise RuntimeError("league pagination did not terminate")
    if not managers:
        raise RuntimeError("league returned no managers")
    return managers
 
 
def fetch_scores(managers, max_gw):
    """Per-manager, per-gameweek points from the authoritative history endpoint.
 
    Full rebuild every run: self-heals missed runs and picks up FPL point
    corrections. Gameweeks before a manager joined are simply absent.
    """
    scores = {}
    for m in managers:
        history = fetch("/entry/%d/history/" % m["entry"])
        gws = {}
        for row in history.get("current", []):
            gw = row.get("event")
            pts = row.get("points")
            if gw is None or pts is None:
                continue
            if 1 <= gw <= max_gw:
                gws[str(gw)] = int(pts)
        scores[str(m["entry"])] = gws
    return scores
 
 
def validate(data):
    assert data["sample"] is False, "sample must be false"
    assert data["league_id"] == LEAGUE_ID, "wrong league id"
    gw = data["last_processed_gw"]
    assert isinstance(gw, int) and 1 <= gw <= 38, "last_processed_gw out of range"
    assert data["managers"], "no managers"
    ids = {str(m["entry"]) for m in data["managers"]}
    assert ids == set(data["scores"]), "managers/scores key mismatch"
    for entry, gws in data["scores"].items():
        assert gws, "manager %s has no scores" % entry
        for k, v in gws.items():
            assert 1 <= int(k) <= gw, "gameweek %s out of range for %s" % (k, entry)
            assert isinstance(v, int), "non-integer points for %s gw %s" % (entry, k)
    json.loads(json.dumps(data))  # round-trips cleanly
 
 
def emit(key, value):
    """Expose a value to later workflow steps."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("%s=%s\n" % (key, value))
 
 
def main():
    existing = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as fh:
            existing = json.load(fh)
 
    # Placeholder data has no authority over last_processed_gw.
    is_sample = bool(existing.get("sample"))
    last_done = 0 if is_sample else int(existing.get("last_processed_gw", 0))
 
    bootstrap = fetch("/bootstrap-static/")
    gw = latest_finished_gw(bootstrap)
 
    if gw == 0:
        print("no new gameweek: no gameweek is finished and data_checked yet")
        emit("updated", "false")
        return 0
 
    if gw <= last_done:
        print("no new gameweek: latest finished is GW%d, already processed" % gw)
        emit("updated", "false")
        return 0
 
    print("processing GW%d (previously processed: GW%d)" % (gw, last_done))
 
    managers = fetch_managers()
    scores = fetch_scores(managers, gw)
 
    data = {
        "sample": False,
        "league_id": LEAGUE_ID,
        "league_name": LEAGUE_NAME,
        "season": SEASON,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_processed_gw": gw,
        "managers": managers,
        "scores": scores,
    }
 
    validate(data)
 
    with open(DATA_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
 
    print("wrote %s for GW%d (%d managers)" % (DATA_FILE, gw, len(managers)))
    emit("updated", "true")
    emit("gw", str(gw))
    return 0
 
 
if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # noqa: BLE001
        # Abort without touching data.json.
        print("ERROR: %s" % err, file=sys.stderr)
        sys.exit(1)
 
