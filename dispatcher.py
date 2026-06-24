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
        print(f"[DISPATCH SUCCESS] {url_key} - Status: {response.status_code}")
    except Exception as e:
        print(f"[DISPATCH ERROR] {url_key} - Failed to fire webhook: {e}")


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
        print(f"[CHAIN DISPATCH] Triggered target_hour={target_hour} - Status: {response.status_code}")
    except Exception as e:
        print(f"[CHAIN DISPATCH ERROR] Failed to chain target_hour={target_hour}: {e}")


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
                        if (now_utc - created_at).total_seconds() < 2 * 3600:
                            is_active = True
                
                if is_active and display_title:
                    active_keys.add(display_title)
            print(f"[DEBUG: GH RUNS] Parsed active workflows titles snapshot: {list(active_keys)}")
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
        
    hour_start = SCRIPT_INIT_TIME.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    
    if target_hour == 23:
        hour_end = hour_start.replace(hour=0) + timedelta(days=1)
    else:
        hour_end = hour_start.replace(hour=target_hour + 1)
        
    if target_hour == 15:
        res = target_time < hour_end
    elif target_hour == 23:
        res = target_time >= hour_start
    else:
        res = hour_start <= target_time < hour_end

    print(f"[DEBUG: SCHEDULE] Responsibility check for target_hour={target_hour} -> Window: [{hour_start.strftime('%H:%M')} to {hour_end.strftime('%H:%M')}]. stream_target={target_time.strftime('%H:%M') if target_time else 'N/A'} -> Result: {res}")
    return res


# Read configuration
with open("data.json", "r") as f:
    data = json.load(f)
    print("Loaded Config Data:", data)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = data["channel_id"]

all_links = data["room_link_n"] + data["room_link_s"] + data["room_link_h"] + data.get("room_link_test", [])

# Check if we are initiator or active monitor
if not TARGET_HOUR_ENV:
    now = datetime.now(jst)
    target_start = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < target_start:
        sleep_seconds = (target_start - now).total_seconds()
        print(f"Initiator mode: Current time is {now.strftime('%H:%M:%S')}. Sleeping for {sleep_seconds:.1f} seconds until 15:00 JST.")
        time.sleep(sleep_seconds)
    else:
        print(f"Initiator mode: Already past 15:00 JST (current: {now.strftime('%H:%M:%S')}). Dispatching 15:00 workflow immediately.")

    next_hour = (now + timedelta(hours=1)).hour
    print(f"[DEBUG: INITIATOR] Calculating chain handoff. Current hour JST: {now.hour}, next target handoff: {next_hour}")
    dispatch_self(next_hour)
    print("Initiator run completed. Exiting.")
    exit(0)

# Active Monitor Mode
target_hour = int(TARGET_HOUR_ENV)
known_schedules = {}          # url_key -> ts or "LIVE"
dispatched_schedules = set()  # url_key only
current_active_runs = set()   # Cached active runs snapshot

script_start_time = datetime.now(jst)
last_fetch_time = None
next_dispatched = False

print(f"Monitoring for target_hour={target_hour} JST")
print("Running for 1.2 hours (72 minutes), fetching API every 1 minute...")

while True:
    now = datetime.now(jst)
    elapsed = (now - script_start_time).total_seconds()

    if elapsed >= 72 * 60:
        print(f"72 minutes have passed (Elapsed: {elapsed:.1f}s). Exiting dispatcher.")
        break

    if elapsed >= 3600 and not next_dispatched:
        if target_hour < 23:
            print(f"[DEBUG: CHAIN] 60-minute mark hit (Elapsed: {elapsed:.1f}s). Handoff triggering for hour: {target_hour + 1}")
            dispatch_self(target_hour + 1)
        next_dispatched = True

    # ---- Fetch API every 1 minute ----
    if last_fetch_time is None or (now - last_fetch_time).total_seconds() >= 60:
        last_fetch_time = now
        print(f"\n--- [{now.strftime('%Y-%m-%d %H:%M:%S')}] STARTING API REFRESH CYCLE ---")

        current_active_runs = get_active_downloading_keys()

        for room_link in all_links:
            try:
                room_api = f"https://public-api.showroom-cdn.com/room/{room_link}"
                res = requests.get(room_api, timeout=10)
                if res.status_code != 200:
                    print(f"[DEBUG: API] Room link {room_link} returned status code {res.status_code}. Skipping.")
                    continue
                result = res.json()
                
                is_live = result.get('is_live')
                next_live = result.get('next_live_schedule')
                print(f"[DEBUG: API] Checked {room_link}: is_live={is_live}, next_live_schedule={next_live}")

                if not isinstance(result, dict):
                    continue

                if "nekojita" in room_api and "乃木坂" not in result.get("name", ""):
                    continue

                url_key = result.get("url_key", room_link)

                ts = None
                if is_live:
                    ts = "LIVE"
                elif next_live:
                    ts = next_live

                if ts:
                    target_time = get_target_time(ts, now)
                    
                    if ts != "LIVE" and not is_responsible_for(target_time, target_hour):
                        print(f"[DEBUG: FILTER] Skipping {url_key} because it falls outside our target hour scope.")
                        continue

                    if known_schedules.get(url_key) != ts:
                        print(f"[DEBUG: STATE] State delta discovered for {url_key}: Old='{known_schedules.get(url_key)}' -> New='{ts}'")
                        known_schedules[url_key] = ts

                        if ts == "LIVE":
                            time_str = "LIVE NOW"
                        else:
                            time_str = datetime.fromtimestamp(ts, tz=jst).strftime("%Y-%m-%d %H:%M")

                        print(f"[NEW SCHEDULE/LIVE DETECTED] {result.get('name', url_key)}: {time_str}")
                        send_telegram_message(
                            TELEGRAM_BOT_TOKEN,
                            TELEGRAM_CHAT_ID,
                            f"{result.get('name', url_key)}\n{time_str}",
                        )

            except Exception as e:
                print(f"Error checking {room_link}: {e}")
        
        print(f"--- [DEBUG: INTERNAL STATE] Current known_schedules: {known_schedules} ---\n")

    # ---- Dispatch check loop (Runs every 10 seconds) ----
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
            print(f"[DEBUG: DISPATCH] Match found! Preparing run logic for '{url_key}' (Condition Status: {ts})")
            
            # Intersect with our active run snapshot to check if a download workflow is already running
            already_running = any(url_key in title for title in current_active_runs)
            print(f"[DEBUG: DISPATCH] Duplication Check for '{url_key}': already_running={already_running}")
            
            if not already_running:
                print(f"[DEBUG: DISPATCH] Triggering dispatch payload execution now for {url_key}...")
                dispatch_download(url_key)
                dispatched_schedules.add(url_key)
            else:
                print(f"[SKIP DISPATCH] Download for {url_key} matches an open GitHub Action run. Will retry on next check if still live.")

    time.sleep(10)