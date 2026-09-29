import json
from websockets.sync.client import connect

with connect("ws://127.0.0.1:8000/ws") as ws:
    for message in ws:
        state = json.loads(message)
        suit = state["suit"]

        if suit["heart_rate_bpm"] > 160:
            print("ALERT: heart rate", suit["heart_rate_bpm"])

        if suit["o2_pri_psi"] < 500:
            print("ALERT: O2 PRI", suit["o2_pri_psi"])

        if suit["o2_sec_psi"] < 500:
            print("ALERT: O2 SEC", suit["o2_sec_psi"])
      