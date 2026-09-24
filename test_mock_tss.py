"""
Runs the Appendix A egress procedure against the sim, fast forwarded.
Takes under a second:  pytest
"""

from fastapi.testclient import TestClient

import mock_tss

# Not used as a context manager, so the live sim loop stays off and we step time by hand
client = TestClient(mock_tss.app)


def flip(panel: str, switch: str, on: bool) -> None:
    r = client.post(f"/{panel}/{switch}", json={"value": on})
    assert r.status_code == 200, r.text


def fast_forward(seconds: float) -> dict:
    for _ in range(int(seconds * mock_tss.TICK_HZ)):
        mock_tss.step(1 / mock_tss.TICK_HZ)
    return client.get("/state").json()


def test_egress_procedure_starts_the_eva():
    client.post("/reset")

    # connect UIA to DCU and start depress
    flip("uia", "emu1_power", True)
    flip("dcu", "batt_umb", True)
    flip("uia", "depress_pump", True)

    # prep O2 tanks
    flip("uia", "o2_vent", True)
    suit = fast_forward(20)["suit"]
    assert suit["o2_pri_psi"] < 10 and suit["o2_sec_psi"] < 10
    flip("uia", "o2_vent", False)

    flip("dcu", "oxy_pri", True)
    flip("uia", "emu1_oxygen", True)
    assert fast_forward(15)["suit"]["o2_pri_psi"] > 3000
    flip("uia", "emu1_oxygen", False)

    flip("dcu", "oxy_pri", False)
    flip("uia", "emu1_oxygen", True)
    assert fast_forward(15)["suit"]["o2_sec_psi"] > 3000
    flip("uia", "emu1_oxygen", False)
    flip("dcu", "oxy_pri", True)

    # end depress, check switches, disconnect
    assert fast_forward(10)["suit"]["suit_pressure_psi"] == 4.0
    flip("uia", "depress_pump", False)
    flip("dcu", "batt_umb", False)
    flip("uia", "emu1_power", False)

    state = fast_forward(10)
    assert state["eva_active"]
    assert state["imu"]["x"] > 0 and state["imu"]["y"] > 0  # walking toward POI A


def test_eva_does_not_start_before_egress():
    client.post("/reset")
    assert not fast_forward(30)["eva_active"]


def test_typo_in_switch_name_is_rejected():
    r = client.post("/uia/o2vent", json={"value": True})
    assert r.status_code == 422
