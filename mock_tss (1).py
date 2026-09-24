"""
mock_tss.py

A tiny fake version of NASA's Telemetry Stream Server (TSS) for onboarding.
It simulates one EV's suit, the UIA and DCU switch panels, and the EV's
position, then serves all of it over HTTP and a WebSocket.

    python mock_tss.py                                 run it
    uvicorn mock_tss:app --reload --host 0.0.0.0       run it, restart on every save
    http://localhost:8000/docs                         poke it from your browser
"""

import asyncio
import math
import random
import socket
from contextlib import asynccontextmanager
from enum import Enum
from typing import Annotated

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, PlainSerializer

TICK_HZ = 5  # sim steps (and WebSocket pushes) per second

# A float that goes out as JSON rounded to 2 decimals, so the numbers stay readable
Num = Annotated[float, PlainSerializer(lambda v: round(v, 2), return_type=float)]


# ===== Data model =====
# This is the contract. panel.py and TssClient.cs expect exactly these names.
# Rename something here and you have to rename it there too.

class Uia(BaseModel):
    emu1_power: bool = False
    depress_pump: bool = False
    o2_vent: bool = False
    emu1_oxygen: bool = False  # the OXYGEN EMU1 valve


class Dcu(BaseModel):
    batt_umb: bool = False   # true = UMB,  false = LOCAL
    oxy_pri: bool = True     # true = PRI,  false = SEC
    comms_a: bool = True     # true = A,    false = B
    fan_pri: bool = True     # true = PRI,  false = SEC
    pump_open: bool = False  # true = OPEN, false = CLOSE
    co2_a: bool = True       # true = A,    false = B


class Suit(BaseModel):
    o2_pri_psi: Num = 800.0
    o2_sec_psi: Num = 750.0
    suit_pressure_psi: Num = 14.7
    batt_pct: Num = 100.0
    heart_rate_bpm: Num = 75.0


class Imu(BaseModel):
    x: Num = 0.0        # meters east of the lander
    y: Num = 0.0        # meters north of the lander
    heading: Num = 0.0  # degrees, 0 = north, 90 = east


class State(BaseModel):
    eva_active: bool = False
    eva_time_s: Num = 0.0
    uia: Uia = Field(default_factory=Uia)
    dcu: Dcu = Field(default_factory=Dcu)
    suit: Suit = Field(default_factory=Suit)
    imu: Imu = Field(default_factory=Imu)


class BoolValue(BaseModel):
    value: bool


class NumberValue(BaseModel):
    value: float


# POIs sit in a loop around the lander, like the real scenario
POIS = [
    {"name": "lander", "x": 0.0, "y": 0.0},
    {"name": "A", "x": 20.0, "y": 25.0},
    {"name": "B", "x": -15.0, "y": 30.0},
    {"name": "C", "x": -25.0, "y": 5.0},
]
WALK_SPEED = 1.5  # meters per second
DWELL_TIME = 8.0  # seconds spent working at each POI

state = State()
next_poi = 1
dwell_left = 0.0
clients: set[WebSocket] = set()


# ===== Simulation =====
# Every rule here is made up but plausible. Enough to make Appendix A work.

def step(dt: float) -> None:
    """Advance the fake physics by dt seconds."""
    uia, dcu, suit = state.uia, state.dcu, state.suit

    # O2 VENT open: both tanks bleed down to 0 psi
    if uia.o2_vent:
        suit.o2_pri_psi = max(0.0, suit.o2_pri_psi - (0.4 * suit.o2_pri_psi + 5) * dt)
        suit.o2_sec_psi = max(0.0, suit.o2_sec_psi - (0.4 * suit.o2_sec_psi + 5) * dt)
    # OXYGEN EMU1 open with power on: fill whichever tank the DCU has selected
    elif uia.emu1_oxygen and uia.emu1_power:
        if dcu.oxy_pri:
            suit.o2_pri_psi = min(3200.0, suit.o2_pri_psi + 250 * dt)
        else:
            suit.o2_sec_psi = min(3200.0, suit.o2_sec_psi + 250 * dt)

    # Breathing pulls O2 from the selected tank. Higher heart rate, more O2.
    breath = 0.02 * suit.heart_rate_bpm * dt
    if dcu.oxy_pri:
        suit.o2_pri_psi = max(0.0, suit.o2_pri_psi - breath)
    else:
        suit.o2_sec_psi = max(0.0, suit.o2_sec_psi - breath)

    # Depress pump (needs EMU power) brings the suit down to 4 psi
    if uia.depress_pump and uia.emu1_power:
        suit.suit_pressure_psi = max(4.0, suit.suit_pressure_psi - 0.35 * dt)

    # Umbilical power charges the battery, local battery drains it
    if dcu.batt_umb and uia.emu1_power:
        suit.batt_pct = min(100.0, suit.batt_pct + 2.0 * dt)
    else:
        suit.batt_pct = max(0.0, suit.batt_pct - 0.05 * dt)

    # The EVA starts once egress is done: EMU power off, local battery, suit at 4 psi
    state.eva_active = (not uia.emu1_power and not dcu.batt_umb
                        and suit.suit_pressure_psi <= 4.05)

    # Heart rate drifts toward a resting or working target, plus some noise
    target = 100.0 if state.eva_active else 75.0
    suit.heart_rate_bpm += (target - suit.heart_rate_bpm) * 0.02 * dt
    suit.heart_rate_bpm += random.gauss(0, 1.0) * math.sqrt(dt)
    suit.heart_rate_bpm = min(190.0, max(45.0, suit.heart_rate_bpm))

    if state.eva_active:
        state.eva_time_s += dt
        walk(dt)


def walk(dt: float) -> None:
    """Move the EV around the POI loop: lander, A, B, C, lander, and so on."""
    global next_poi, dwell_left
    if dwell_left > 0:  # still working at a POI
        dwell_left -= dt
        return
    imu, goal = state.imu, POIS[next_poi]
    dx, dy = goal["x"] - imu.x, goal["y"] - imu.y
    dist = math.hypot(dx, dy)
    if dist < 0.5:  # arrived: work for a bit, then head to the next one
        next_poi = (next_poi + 1) % len(POIS)
        dwell_left = DWELL_TIME
        return
    stride = min(dist, WALK_SPEED * dt)
    imu.x += dx / dist * stride
    imu.y += dy / dist * stride
    imu.heading = math.degrees(math.atan2(dx, dy)) % 360


# ===== Server =====

async def sim_loop() -> None:
    """Step the sim and push the new state to every WebSocket client."""
    dt = 1 / TICK_HZ
    while True:
        step(dt)
        payload = state.model_dump_json()
        for ws in list(clients):
            try:
                await ws.send_text(payload)
            except Exception:
                clients.discard(ws)  # that client left
        await asyncio.sleep(dt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sim_loop())  # sim starts when the server starts
    yield
    task.cancel()


app = FastAPI(title="Mock TSS", lifespan=lifespan)

# Enums of the field names, so /docs shows a dropdown and typos get a clear error
UiaSwitch = Enum("UiaSwitch", {name: name for name in Uia.model_fields}, type=str)
DcuSwitch = Enum("DcuSwitch", {name: name for name in Dcu.model_fields}, type=str)
SuitField = Enum("SuitField", {name: name for name in Suit.model_fields}, type=str)

# Every endpoint is async def, so everything runs on one thread and no locks are needed.


@app.get("/state")
async def get_state() -> State:
    """Everything at once. Poll this if you can't use the WebSocket."""
    return state


@app.get("/pois")
async def get_pois() -> dict:
    """POI coordinates in meters from the lander."""
    return {"pois": POIS}


@app.post("/uia/{switch}")
async def set_uia(switch: UiaSwitch, body: BoolValue) -> Uia:
    """Flip a UIA switch."""
    setattr(state.uia, switch.value, body.value)
    return state.uia


@app.post("/dcu/{switch}")
async def set_dcu(switch: DcuSwitch, body: BoolValue) -> Dcu:
    """Flip a DCU switch."""
    setattr(state.dcu, switch.value, body.value)
    return state.dcu


@app.post("/debug/suit/{field}")
async def inject(field: SuitField, body: NumberValue) -> Suit:
    """Force a suit value. Handy for testing caution and warning."""
    setattr(state.suit, field.value, body.value)
    return state.suit


@app.post("/reset")
async def reset() -> State:
    """Start the scenario over."""
    global state, next_poi, dwell_left
    state, next_poi, dwell_left = State(), 1, 0.0
    return state


@app.websocket("/ws")
async def stream(ws: WebSocket) -> None:
    """Pushes the full state TICK_HZ times per second. Same JSON as GET /state."""
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # incoming messages are ignored; this just waits for a disconnect
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


def lan_ip() -> str:
    """Best guess at this laptop's IP on the local network, for headsets."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP connect sends no packets, it just picks a route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    print("\n  docs:     http://localhost:8000/docs")
    print(f"  headset:  set TssClient host to {lan_ip()}\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
