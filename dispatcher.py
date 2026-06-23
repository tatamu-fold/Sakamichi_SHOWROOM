import os
import requests
import json
from datetime import datetime, timedelta
import pytz
from telegram import send_telegram_message, send_telegram_file
import time

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
TARGET_REPO = os.getenv("TARGET_REPO")
TARGET_HOUR_ENV = os.getenv("TARGET_HOUR", "")

API_URL = f"https://api.github.com/repos/{TARGET_REPO}/dispatches"
jst = pytz.timezone("Asia/Tokyo")

# Lock the baseline date at script initialization to prevent day/hour shifting during runtime
SCRIPT_INIT_TIME = datetime.now(jst)


def check_day_relation_jst(timestamp: int) -> str:
    target_date = datetime.fromtimestamp(timestamp, jst).date()
    today_date = datetime.now(jst).date()
    if target_date == today_date:
        return "today"
    else:
        return "future"


def dispatch_download(url_key):
    payload = {
        "event_type": "trigger-download",
        "client_payload": {"url_key": str(url_key)},
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=10)
        print(f"[DISPATCH] {url_key} - Status:", response.status_code)
    except Exception as e:
        print(f"[DISPATCH ERROR] {url_key} - {e}")


def dispatch_self(target_hour: int):
    url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/dispatcher.yml/dispatches"
    ref = os.getenv("GITHUB_REF_NAME", "main")
    payload = {
        "ref": ref,
        "inputs": {"target_hour": str(target_hour)},
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[CHAIN DISPATCH] Triggered target_hour={target_hour} - Status:", response.status_code)
    except Exception as e:
        print(f"[CHAIN DISPATCH ERROR] {e}")


# Optimization: Fetch all active runs once per cycle to prevent GitHub API rate limit exhaustion
def get_active_downloading_keys():
    active_keys = set()
    url = f"https://api.github.com/repos/{TARGET_REPO}/actions/runs?per_page=50"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            runs = response.json().get("workflow_runs", [])
            now_utc = datetime.now(pytz.UTC)
            for run in runs:
                display_title = run.get("display_title", "")
                status = run.get("status", "")
                
                is_active = status in ["in_progress", "queued"]
                if not is_active:
                    created_at_str = run.get("created_at")
                    if created_at_str:
                        created_at = datetime.strptime(created_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
                        # Mark as active if it completed within the last 2 hours to avoid race duplicates
                        if (now_utc - created_at).total_seconds() < 2 * 3600:
                            is_active = True
                
                if is_active and display_title:
                    active_keys.add(display_title)
        else:
            print(f"[WARN] Failed to fetch workflow runs: {response.status_code}")
    except Exception as e:
        print(f"[WARN] Error checking workflow runs: {e}")
    return active_keys


def get_target_time(ts, jst_now):
    if ts == "LIVE":
        return jst_now
    try:
        return datetime.fromtimestamp(int(ts), jst) - timedelta(minutes=15)
    except Exception:
        return None


def is_responsible_for(target_time, target_hour):
    if target_time is None:
        return False
        
    # Build absolute intervals using the firm script initialization date to eliminate time-drift
    hour_start = SCRIPT_INIT_TIME.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    
    # Handle cross-day boundary transition elegantly
    if target_hour == 23:
        hour_end = hour_start.replace(hour=0) + timedelta(days=1)
    else:
        hour_end = hour_start.replace(hour=target_hour + 1)
        
    if target_hour == 15:
        return target_time < hour_end
    elif target_hour == 22:
        return target_time >= hour_start
    else:
        return hour_start <= target_time < hour_end


# Read configuration
with open("data.json", "r") as f:
    data = json.load(f)
    print(data)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = data["channel_id"]

all_links = data["room_link_n"] + data["room_link_s"] + data["room_link_h"]

# Check if we are initiator or active monitor
if not TARGET_HOUR_ENV:
    # Initiator mode: runs at 13:00 JST, waits until 15:00 JST to invoke the first monitor instance
    now = datetime.now(jst)
    target_start = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < target_start:
        sleep_seconds = (target_start - now).total_seconds()
        print(f"Initiator mode: Current time is {now.strftime('%H:%M:%S')}. Sleeping for {sleep_seconds:.1f} seconds until 15:00 JST.")
        time.sleep(sleep_seconds)
    else:
        print(f"Initiator mode: Already past 15:00 JST (current: {now.strftime('%H:%M:%S')}). Dispatching 15:00 workflow immediately.")

    dispatch_self(15)
    print("Initiator run completed. Exiting.")
    exit(0)

# Active Monitor Mode
target_hour = int(TARGET_HOUR_ENV)
known_schedules = {}          # url_key -> ts or "LIVE"
dispatched_schedules = set()  # url_key only

script_start_time = datetime.now(jst)
last_fetch_time = None
next_dispatched = False

print(f"Monitoring for target_hour={target_hour} JST")
print("Running for 1.33 hours (80 minutes), fetching API every 1 minute...")

while True:
    now = datetime.now(jst)
    elapsed = (now - script_start_time).total_seconds()

    # Safety: Extended from 72 to 80 minutes to establish a robust 20-minute overlap cushion
    if elapsed >= 80 * 60:
        print("80 minutes have passed. Exiting dispatcher.")
        break

    # Trigger next hour's runner right at the 60-minute mark (extended past 22 to secure late night handoffs)
    if elapsed >= 3600 and not next_dispatched:
        if target_hour < 23:
            dispatch_self(target_hour + 1)
        next_dispatched = True

    # ---- Fetch API every 1 minute ----
    if last_fetch_time is None or (now - last_fetch_time).total_seconds() >= 60:
        last_fetch_time = now
        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Fetching schedules from API...")

        # Snapshot current GitHub Action runs once per minute to conserve API rate limits
        current_active_runs = get_active_downloading_keys()

        for room_link in all_links:
            try:
                room_api = f"https://public-api.showroom-cdn.com/room/{room_link}"
                res = requests.get(room_api, timeout=10)
                if res.status_code != 200:
                    continue
                result = res.json()

                if not isinstance(result, dict):
                    continue

                if "nekojita" in room_api and "乃木坂" not in result.get("name", ""):
                    continue

                url_key = result.get("url_key", room_link)

                ts = None
                if result.get("is_live"):
                    ts = "LIVE"
                elif result.get("next_live_schedule"):
                    ts = result["next_live_schedule"]

                if ts:
                    target_time = get_target_time(ts, now)
                    # Filter: check if this specific stream falls within our designated responsibility segment
                    if not is_responsible_for(target_time, target_hour):
                        continue

                    if known_schedules.get(url_key) != ts:
                        known_schedules[url_key] = ts

                        if ts == "LIVE":
                            time_str = "LIVE NOW"
                        else:
                            time_str = datetime.fromtimestamp(ts, tz=jst).strftime("%Y-%m-%d %H:%M")

                        print(f"[NEW SCHEDULE/LIVE] {result.get('name', url_key)}: {time_str}")
                        send_telegram_message(
                            TELEGRAM_BOT_TOKEN,
                            TELEGRAM_CHAT_ID,
                            f"{result.get('name', url_key)}\n{time_str}",
                        )

            except Exception as e:
                print(f"Error checking {room_link}: {e}")

        # ---- Dispatch check loop ----
        for url_key, ts in list(known_schedules.items()):
            if url_key in dispatched_schedules:
                continue

            should_dispatch = False
            if ts == "LIVE":
                should_dispatch = True
            else:
                target_time = datetime.fromtimestamp(ts, jst) - timedelta(minutes=15)
                if now >= target_time:
                    should_dispatch = True

            if should_dispatch:
                # Intersect with our active run snapshot to check if a download workflow is already running
                already_running = any(url_key in title for title in current_active_runs)
                
                if not already_running:
                    dispatch_download(url_key)
                else:
                    print(f"[SKIP DISPATCH] Download for {url_key} is already active or recently ran in GitHub Actions.")
                dispatched_schedules.add(url_key)

    time.sleep(10)