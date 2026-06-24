import os
import requests
import json
from datetime import datetime, timedelta
import pytz
from telegram import send_telegram_message, send_telegram_file
import time
import subprocess
import threading
from m3u8_ts_to_tg import M3U8TSToTG

with open("data.json", "r") as f:
    data = json.load(f)
    print(data)


# JSON 文件存储每个文件的状态（首次出现时间 + 是否已发送）
SENT_JSON_FILE = "sent.json"
url_key = os.getenv("url_key")

if url_key in data["room_link_n"]:
    channel_id = data["channel_id_n"]
elif url_key in data["room_link_s"]:
    channel_id = data["channel_id_s"]
elif url_key in data["room_link_h"]:
    channel_id = data["channel_id_h"]
elif url_key in data.get("room_link_test", []):
    channel_id = data["channel_id_test"]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = channel_id

room_link = f"https://public-api.showroom-cdn.com/room/{url_key}"
room_link_result = requests.get(room_link).json()
api_link = f"https://www.showroom-live.com/api/live/streaming_url?room_id={room_link_result['id']}"

jst = pytz.timezone("Asia/Tokyo")
today_str = datetime.now(jst).strftime("%Y%m%d")


def retry_command_until_success(command, max_retries=10, retry_interval=5):
    for attempt in range(1, max_retries + 1):
        print(f"[Thread] Attempt {attempt}: Running command...")
        process = subprocess.Popen(command, shell=True)
        process.wait()
        if process.returncode == 0:
            print("[Thread] Command succeeded.")
            return
        else:
            print(
                f"[Thread] Failed with return code {process.returncode}. Retrying in {retry_interval}s..."
            )
            time.sleep(retry_interval)
    print("[Thread] Max retries reached. Command failed.")


if __name__ == "__main__":
    while True:
        try:
            m3u8_result = requests.get(api_link).json()
            
            m3u8_url = next(
                s["url"]
                for s in m3u8_result["streaming_url_list"]
                if s["type"] == "hls" and "main_ss.m3u8" in s["url"]
            )

            break
        except:
            time.sleep(5)
    # m3u8_url = "https://hls-css.live.showroom-live.com/live/xx.m3u8".replace("_abr", "")
    # command = f'./N_m3u8DL-RE --live-real-time-merge "{m3u8_url}" --save-name chunklist'
    # t = threading.Thread(target=retry_command_until_success, args=(command, 100, 5))
    # t.start()

    # process = subprocess.Popen(command, shell=True)
    m3u8_processor = M3U8TSToTG(
        m3u8_url=m3u8_url,  # URL will be fetched from API
        telegram_bot_token=TELEGRAM_BOT_TOKEN,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        caption_prefix=url_key,
        work_dir=".",
        merge_group_size=5 if "nekojita" in url_key else 15,
    )
    m3u8_processor.run()
