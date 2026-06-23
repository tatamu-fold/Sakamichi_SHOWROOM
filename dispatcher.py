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
    response = requests.post(API_URL, json=payload, headers=headers)
    print(f"[DISPATCH] {url_key} - Status:", response.status_code)


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
    response = requests.post(url, json=payload, headers=headers)
    print(f"[CHAIN DISPATCH] Triggered target_hour={target_hour} - Status:", response.status_code)
    try:
        print("Response:", response.text)
    except Exception:
        pass


def is_already_downloading(url_key):
    url = f"https://api.github.com/repos/{TARGET_REPO}/actions/runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            runs = response.json().get("workflow_runs", [])
            for run in runs:
                display_title = run.get("display_title", "")
                status = run.get("status", "")
                # Check if this run is for our url_key
                if url_key in display_title:
                    # If it's currently running, queued, or completed in the last 2 hours
                    if status in ["in_progress", "queued"]:
                        return True
                    
                    created_at_str = run.get("created_at")
                    if created_at_str:
                        # Parse UTC timestamp (e.g. 2026-06-23T02:18:27Z)
                        created_at = datetime.strptime(created_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
                        now_utc = datetime.now(pytz.UTC)
                        if (now_utc - created_at).total_seconds() < 2 * 3600:
                            return True
        else:
            print(f"[WARN] Failed to fetch workflow runs: {response.status_code}")
    except Exception as e:
        print(f"[WARN] Error checking workflow runs: {e}")
    return False


def get_target_time(ts, jst_now):
    if ts == "LIVE":
        return jst_now
    try:
        return datetime.fromtimestamp(int(ts), jst) - timedelta(minutes=15)
    except Exception:
        return None


def is_responsible_for(target_time, target_hour, jst_now):
    if target_time is None:
        return False
        
    base_dt = jst_now.replace(minute=0, second=0, microsecond=0)
    hour_start = base_dt.replace(hour=target_hour)
    
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
    # Initiator mode: run at 13:00 JST, wait until 15:00 JST to call the first monitor run
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
print("Running for 1.2 hours (72 minutes), fetching API every 1 minute...")

while True:
    now = datetime.now(jst)
    elapsed = (now - script_start_time).total_seconds()

    if elapsed >= 72 * 60:
        print("1.2 hours have passed. Exiting dispatcher.")
        break

    # Trigger next hour's run at the 60-minute mark
    if elapsed >= 3600 and not next_dispatched:
        if target_hour < 22:
            dispatch_self(target_hour + 1)
        next_dispatched = True

    # ---- Fetch API every 1 minute ----
    if last_fetch_time is None or (now - last_fetch_time).total_seconds() >= 60:
        last_fetch_time = now
        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Fetching schedules from API...")

        for room_link in all_links:
            try:
                room_api = f"https://public-api.showroom-cdn.com/room/{room_link}"
                result = requests.get(room_api).json()

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
                    # Filter: only keep schedules within our target hour's window
                    if not is_responsible_for(target_time, target_hour, now):
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
            continue  # already dispatched once in this run

        should_dispatch = False

        if ts == "LIVE":
            should_dispatch = True
        else:
            target_time = datetime.fromtimestamp(ts, jst) - timedelta(minutes=15)
            if now >= target_time:
                should_dispatch = True

        if should_dispatch:
            if not is_already_downloading(url_key):
                dispatch_download(url_key)
            else:
                print(f"[SKIP DISPATCH] Download for {url_key} is already active or recently ran.")
            dispatched_schedules.add(url_key)

    time.sleep(10)
