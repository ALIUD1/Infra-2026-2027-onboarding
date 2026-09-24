"""
panel.py

The second script. It plays the physical UIA and DCU switch panels: flip
switches, inject faults, and watch the live telemetry stream from mock_tss.py.

    python panel.py                   server on this laptop
    python panel.py 192.168.1.42      server on another machine
"""

import json
import sys

import requests
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
BASE = f"http://{HOST}:8000"
WS_URL = f"ws://{HOST}:8000/ws"

WORDS = {"on": True, "open": True, "true": True, "1": True,
         "off": False, "close": False, "false": False, "0": False}

HELP = """\
  status                    everything, once
  watch                     live stream (Ctrl+C to stop)
  uia <switch> on|off       uia o2_vent open
  dcu <switch> on|off       dcu oxy_pri off      (off means SEC)
  inject <field> <number>   inject heart_rate_bpm 175
  reset                     start the scenario over
  quit"""


def one_line(st: dict) -> str:
    """Squash the state into one short line for the live view."""
    s, p = st["suit"], st["imu"]
    phase = f"EVA {st['eva_time_s']:4.0f}s" if st["eva_active"] else "pre EVA  "
    return (f"{phase} | O2 {s['o2_pri_psi']:4.0f}/{s['o2_sec_psi']:4.0f}"
            f" | suit {s['suit_pressure_psi']:5.2f} | batt {s['batt_pct']:3.0f}%"
            f" | HR {s['heart_rate_bpm']:3.0f} | xy {p['x']:5.1f},{p['y']:5.1f}")


def status() -> None:
    st = requests.get(f"{BASE}/state", timeout=2).json()
    print(one_line(st) + "   (O2 and suit in psi)")
    for panel in ("uia", "dcu"):
        flags = "  ".join(f"{name}={'ON' if on else 'off'}" for name, on in st[panel].items())
        print(f"  {panel.upper()}  {flags}")


def watch() -> None:
    print("streaming (Ctrl+C to stop)")
    try:
        with connect(WS_URL) as ws:
            for message in ws:  # blocks until the server pushes the next update
                print("\r" + one_line(json.loads(message)), end="", flush=True)
    except KeyboardInterrupt:
        pass
    print()


def post(path: str, value) -> None:
    r = requests.post(f"{BASE}{path}", json={"value": value}, timeout=2)
    if r.ok:
        print("ok")
        return
    detail = r.json().get("detail")  # FastAPI explains what was wrong in "detail"
    if isinstance(detail, list):
        detail = "; ".join(d.get("msg", "") for d in detail)
    print(f"error {r.status_code}: {detail}")


def main() -> None:
    print(f"panel talking to {BASE}. type help for commands")
    while True:
        try:
            cmd = input("> ").split()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not cmd:
            continue
        try:
            if cmd[0] == "status":
                status()
            elif cmd[0] == "watch":
                watch()
            elif cmd[0] in ("uia", "dcu") and len(cmd) == 3 and cmd[2].lower() in WORDS:
                post(f"/{cmd[0]}/{cmd[1]}", WORDS[cmd[2].lower()])
            elif cmd[0] == "inject" and len(cmd) == 3:
                post(f"/debug/suit/{cmd[1]}", float(cmd[2]))
            elif cmd[0] == "reset":
                requests.post(f"{BASE}/reset", timeout=2)
                print("scenario reset")
            elif cmd[0] in ("quit", "exit"):
                break
            else:
                print(HELP)
        except (OSError, WebSocketException):  # connection refused, timed out, dropped
            print(f"can't reach {BASE}. is mock_tss.py running?")
        except ValueError:
            print("bad value, type help")


if __name__ == "__main__":
    main()
