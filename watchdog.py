import json
from websockets.sync.client import connect

with connect("ws://localhost:8000/ws") as ws:
    print("watchdog connected")
    for msg in ws:
        suit = json.loads(msg)["suit"]
        if suit["heart_rate_bpm"] > 160:
            print("ALERT: heart rate", suit["heart_rate_bpm"])
        if suit["o2_pri_psi"] < 500 or suit["o2_sec_psi"] < 500:
            print("ALERT: low O2", suit["o2_pri_psi"], suit["o2_sec_psi"])

            