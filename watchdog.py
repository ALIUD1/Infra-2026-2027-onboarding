# watchdog.py
import json
from websockets.sync.client import connect
with connect("ws://127.0.0.1:8000/ws") as ws:
    for message in ws:
        data = json.loads(message)
        suit = data.get("suit", {})
        hr = suit.get("heart_rate_bpm", 0)
        o2_pri = suit.get("o2_pri_psi", 0)
        o2_sec = suit.get("o2_sec_psi", 0)

        if hr > 160:
            print(f"High Heart Rate! ({hr:.1f} bpm)")
        if o2_pri < 500:
            print(f"Primary O2 Critical! ({o2_pri:.1f} psi)")
        if o2_sec < 500:
            print(f"Secondary O2 Critical! ({o2_sec:.1f} psi)")