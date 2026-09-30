import json
from websockets.sync.client import connect

with connect("ws://localhost:8000/ws") as ws:
            print("Watchdog connected")
            for message in ws:
                state = json.loads(message)
                suit = state["suit"]
                if suit["heart_rate_bpm"] > 160:
                    print("Warning: Heart rate too high!", suit["heart_rate_bpm"])
                if suit["o2_pri_psi"] < 500:
                    print("Warning: O2 tank levels too low!", suit["o2_pri_psi"])
                if suit["o2_sec_psi"] < 500:
                    print("Warning: Secondary O2 tank levels too low!", suit["o2_sec_psi"])
                  
