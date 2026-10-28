import json, time, urllib.request

def call(port, path, body=None):
    req = urllib.request.Request(
        f"http://localhost:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    return json.load(urllib.request.urlopen(req))

def drive(port):
    call(port, "/reset", {})
    call(port, "/debug/suit/suit_pressure_psi", {"value": 4.0})
    time.sleep(6)
    return call(port, "/state")

py, go = drive(8000), drive(8001)
print("eva_active  py/go:", py["eva_active"], go["eva_active"])
print("eva_time_s  py/go:", py["eva_time_s"], go["eva_time_s"])
print("imu         py:", py["imu"])
print("imu         go:", go["imu"])
print("heading match:", py["imu"]["heading"] == go["imu"]["heading"])
print("key sets match:", set(py) == set(go) and set(py["suit"]) == set(go["suit"]))
