import sys
import json

from websockets.sync.client import connect

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
if ":" not in HOST:
    HOST += ":8000"
WS_URL = f"ws://{HOST}/ws"

def main() -> None:
    print("streaming (Ctrl+C to stop)")
    try:
        with connect(WS_URL) as ws:
            for message in ws:  # blocks until the server pushes the next update
                # print("\r" + one_line(json.loads(message)), end="", flush=True)
                st = json.loads(message)
                s = st["suit"]
                heart_rate_bpm = s["heart_rate_bpm"]
                o2_pri_psi = s["o2_pri_psi"]
                if heart_rate_bpm > 160:
                    print(f"Alert: heart_rate_bpm is {heart_rate_bpm} which is above 160")
                if o2_pri_psi < 500:
                    print(f"Alert: o2_pri_psi is {o2_pri_psi} which is below 500")
    except KeyboardInterrupt:
        pass
    print()

if __name__ == "__main__":
    main()
