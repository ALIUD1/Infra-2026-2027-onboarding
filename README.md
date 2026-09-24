# CLAWS Onboarding: Mock TSS

At test week our display pulls live data from NASA's Telemetry Stream Server (TSS). This repo is a small fake TSS so you can learn that whole data path on your own laptop: a Python server, a second script talking to it, and Unity listening in.

```
panel.py       →  HTTP POST  →   mock_tss.py    →  WebSocket  →   TssClient.cs (Unity)
the switches                     the fake TSS                     the HMD
```

## What's here

| File | What it is |
|---|---|
| `mock_tss.py` | FastAPI server. Simulates the suit, UIA, DCU, and the EV walking between POIs. |
| `panel.py` | The second script. Flip switches, inject faults, watch the live stream. |
| `TssClient.cs` | Unity script. Streams telemetry in and sends switch flips back out. |
| `test_mock_tss.py` | Runs the whole egress procedure in under a second. |

## Setup

Python 3.10+ and Unity 2021 LTS or newer (Unity 6 works).

```
git clone https://github.com/ALIUD1/Infra-2026-2027-onboarding.git
cd Infra-2026-2027-onboarding
python -m venv .venv
```

Activate it: `.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Mac/Linux. Do this in every new terminal you open for this repo. Then:

```
pip install -r requirements.txt
python mock_tss.py
```

Open http://localhost:8000/docs. If that page loads, you're set.

On a Mac, `python` might be called `python3`. If `mock_tss.py` crashes with "address already in use" or `WinError 10048`, see [When it doesn't work](#when-it-doesnt-work).

## Missions

### 0. Poke it (5 min)

In `/docs`, open **GET /state**, click Try it out, then Execute. Next open **POST /uia/{switch}**, pick `o2_vent`, send `{"value": true}`, and GET the state again. The O2 tanks are dropping. You just talked to a server without writing any code.

### 1. Two scripts talking (15 min)

1. Second terminal: `python panel.py`, then `watch`.
2. Third terminal: `python panel.py`, then `uia o2_vent open` and `inject heart_rate_bpm 175`. Watch the second terminal react.
3. **Write your own client.** Make `watchdog.py` (about 15 lines) that connects to `ws://localhost:8000/ws` and prints an alert whenever heart rate goes over 160 or either O2 tank drops below 500 psi. Test it with `inject`. Hint: `watch()` in panel.py is most of the answer.

Run `reset` in the panel when you're done.

### 2. Unity (15 min)

1. New project from any 3D template. Copy `TssClient.cs` into `Assets`.
2. Edit → Project Settings → Player → Other Settings → **Allow downloads over HTTP** → **Always allowed**. Unity blocks plain `http://` by default, and without this the toggles in step 6 fail with "Insecure connection not allowed".
3. Create an empty GameObject, add the **TssClient** component, press Play. Live numbers show up top left.
4. Add a Cube and drag it into **Ev Marker**.
5. Set the Main Camera to position (0, 20, 0) and rotation (90, 0, 0). Now you're looking at a top down map with north up.
6. Click a toggle in the debug panel. Unity just sent data to the server, and `watch` in your terminal shows it.

### 3. Run the real egress (15 min)

This is Appendix A of the mission description. Start with `reset`, then run it from `panel.py` (or the Unity toggles) and watch the numbers respond:

```
# connect UIA to DCU, start depress
uia emu1_power on
dcu batt_umb on                 (UMB)
uia depress_pump on

# prep O2 tanks
uia o2_vent open                wait until both tanks are under 10 psi
uia o2_vent close
dcu oxy_pri on                  (PRI)
uia emu1_oxygen open            wait until primary is over 3000 psi
uia emu1_oxygen close
dcu oxy_pri off                 (SEC)
uia emu1_oxygen open            wait until secondary is over 3000 psi
uia emu1_oxygen close
dcu oxy_pri on                  (PRI)

# end depress, check switches, disconnect
                                wait until suit pressure is 4 psi
uia depress_pump off
dcu batt_umb off                (LOCAL)
uia emu1_power off
status                          verify comms_a, fan_pri, co2_a are ON and pump_open is off
```

Once egress is done the EVA starts and the EV walks the POI loop (lander, A, B, C, back to the lander). Your cube follows it.

That's it. You've now run the same data path we'll use at test week: a server, a client, and Unity, all talking over the network.

## API

| | Path | Notes |
|---|---|---|
| GET | `/state` | everything |
| GET | `/pois` | POI coordinates, meters from the lander |
| POST | `/uia/{switch}` | body `{"value": true}` |
| POST | `/dcu/{switch}` | body `{"value": true}` |
| POST | `/debug/suit/{field}` | force a value, body `{"value": 175}` |
| POST | `/reset` | start the scenario over |
| WS | `/ws` | same JSON as `/state`, pushed 5 times a second |

Sample `/state`:

```json
{
  "eva_active": false,
  "eva_time_s": 0.0,
  "uia":  {"emu1_power": false, "depress_pump": false, "o2_vent": false, "emu1_oxygen": false},
  "dcu":  {"batt_umb": false, "oxy_pri": true, "comms_a": true, "fan_pri": true, "pump_open": false, "co2_a": true},
  "suit": {"o2_pri_psi": 795.78, "o2_sec_psi": 750.0, "suit_pressure_psi": 14.7, "batt_pct": 99.86, "heart_rate_bpm": 75.09},
  "imu":  {"x": 0.0, "y": 0.0, "heading": 0.0}
}
```

For DCU switches the name says what `true` means, so `batt_umb: false` is LOCAL and `oxy_pri: false` is SEC.

## When it doesn't work

* **"address already in use" / `WinError 10048` when starting the server.** Something else on your laptop already uses port 8000. Close it, or run the server on another port with `uvicorn mock_tss:app --host 0.0.0.0 --port 8001`, then `python panel.py 127.0.0.1:8001`, and set **Port** to 8001 on the TssClient component.
* **`No module named ...`.** Your venv isn't active in that terminal. Activate it (see Setup) and try again.
* **Headset can't connect.** Use your laptop's IP, not localhost (mock_tss.py prints it on startup). Both devices need to be on the same network, and campus WiFi often blocks device to device traffic, so a phone hotspot or travel router is the easy fix. Let Python through your firewall when it asks.
* **"Insecure connection not allowed" in Unity.** Player Settings → Other Settings → Allow downloads over HTTP → Always allowed. This only affects UnityWebRequest (polling and switch flips), not the WebSocket.
* **A value is stuck at 0 in Unity.** JsonUtility silently skips fields it can't match. Check the spelling in the C# classes and that they're `[Serializable]`.
* **Errors about the main thread.** Unity APIs only work on the main thread. That's why the script hands JSON to `Update()` instead of touching objects from the network code. Keep that pattern when you add features.
* **Quest or other Android builds.** Set Internet Access to Require. **HoloLens:** turn on the InternetClient and PrivateNetworkClientServer capabilities.
* **Debug panel missing on a headset.** OnGUI is an editor tool. Real HMD UI should read `TssClient.Latest`.
* **Want the server to restart every time you save?** Run `uvicorn mock_tss:app --reload --host 0.0.0.0` instead.

## Tests

`pytest` runs the whole egress procedure against the sim, fast forwarded, in under a second. You should see `3 passed`. A deprecation warning about `httpx` is harmless.
