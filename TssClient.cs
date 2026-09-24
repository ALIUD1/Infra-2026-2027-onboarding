// TssClient.cs
// Streams live telemetry from mock_tss.py into Unity and sends switch flips back.
//
// Setup: put this on any GameObject and press Play.
// Other scripts read TssClient.Latest, or call SetSwitch("uia", "o2_vent", true).

using System;
using System.Collections;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.Networking;

// These mirror the JSON from mock_tss.py. JsonUtility matches fields by exact
// name, which is why they are snake_case. A misspelled or missing field does
// NOT throw an error, it just stays 0 or false. Check here first when a value
// looks stuck.
[Serializable] public class TssUia  { public bool emu1_power, depress_pump, o2_vent, emu1_oxygen; }
[Serializable] public class TssDcu  { public bool batt_umb, oxy_pri, comms_a, fan_pri, pump_open, co2_a; }
[Serializable] public class TssSuit { public float o2_pri_psi, o2_sec_psi, suit_pressure_psi, batt_pct, heart_rate_bpm; }
[Serializable] public class TssImu  { public float x, y, heading; }
[Serializable] public class TssState
{
    public bool eva_active;
    public float eva_time_s;
    public TssUia uia;
    public TssDcu dcu;
    public TssSuit suit;
    public TssImu imu;
}
[Serializable] public class TssSwitchBody { public bool value; }

public class TssClient : MonoBehaviour
{
    [Header("Server")]
    [Tooltip("127.0.0.1 in the editor. On a headset, your laptop's IP (mock_tss.py prints it).")]
    public string host = "127.0.0.1";
    public int port = 8000;
    [Tooltip("On: the server pushes every update over a WebSocket. Off: poll GET /state.")]
    public bool useWebSocket = true;
    public float pollSeconds = 0.25f;

    [Header("Optional")]
    [Tooltip("Any object (a cube works). It follows the EV around.")]
    public Transform evMarker;
    [Tooltip("Unity units per meter")]
    public float worldScale = 0.25f;
    public bool showDebugPanel = true;

    // Newest telemetry. Null until the first message arrives.
    public TssState Latest { get; private set; }
    public float SecondsSinceUpdate => Time.time - lastUpdateTime;

    string HttpBase => $"http://{host}:{port}";
    CancellationTokenSource cts;
    string pendingJson;   // newest raw message, handed from the network code to Update()
    float lastUpdateTime;
    string lastError;

    void Start()
    {
        cts = new CancellationTokenSource();
        if (useWebSocket) _ = StreamLoop(cts.Token);
        else StartCoroutine(PollLoop());
    }

    void OnDestroy() => cts?.Cancel();

    void Update()
    {
        // Parse here, on the main thread. Unity objects can only be touched from the main thread.
        if (pendingJson != null)
        {
            Latest = JsonUtility.FromJson<TssState>(pendingJson);
            pendingJson = null;
            lastUpdateTime = Time.time;
        }

        if (Latest != null && evMarker != null)
        {
            // TSS x/y is a flat map (x east, y north). In Unity the ground is x/z.
            var target = new Vector3(Latest.imu.x, 0f, Latest.imu.y) * worldScale;
            evMarker.position = Vector3.Lerp(evMarker.position, target, 10f * Time.deltaTime);
            evMarker.rotation = Quaternion.Euler(0f, Latest.imu.heading, 0f);
        }
    }

    // ===== Receiving, option 1: WebSocket (the server pushes each update) =====
    async Task StreamLoop(CancellationToken token)
    {
        var buffer = new byte[8192];
        while (!token.IsCancellationRequested)
        {
            try
            {
                using (var ws = new ClientWebSocket())
                using (var message = new MemoryStream())
                {
                    await ws.ConnectAsync(new Uri($"ws://{host}:{port}/ws"), token);
                    Debug.Log("[TSS] stream connected");
                    lastError = null;
                    while (ws.State == WebSocketState.Open)
                    {
                        var result = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), token);
                        if (result.MessageType == WebSocketMessageType.Close) break;
                        message.Write(buffer, 0, result.Count);
                        if (!result.EndOfMessage) continue;  // big messages can arrive in pieces
                        pendingJson = Encoding.UTF8.GetString(message.ToArray());
                        message.SetLength(0);
                    }
                }
            }
            catch (Exception e)
            {
                if (token.IsCancellationRequested) return;  // we're shutting down, not an error
                if (e.Message != lastError) Debug.LogWarning($"[TSS] {e.Message} (retrying every 2 s)");
                lastError = e.Message;
            }

            try { await Task.Delay(2000, token); }
            catch (OperationCanceledException) { return; }
        }
    }

    // ===== Receiving, option 2: polling (simplest, works on every platform) =====
    IEnumerator PollLoop()
    {
        var wait = new WaitForSeconds(pollSeconds);
        while (true)
        {
            using (var req = UnityWebRequest.Get($"{HttpBase}/state"))
            {
                yield return req.SendWebRequest();
                if (req.result == UnityWebRequest.Result.Success)
                {
                    pendingJson = req.downloadHandler.text;
                    lastError = null;
                }
                else if (req.error != lastError)
                {
                    lastError = req.error;
                    Debug.LogWarning($"[TSS] poll failed: {req.error}");
                }
            }
            yield return wait;
        }
    }

    // ===== Sending =====
    // panel is "uia" or "dcu". name is a field name like "o2_vent".
    public void SetSwitch(string panel, string name, bool value)
    {
        // JsonUtility writes {"value":true}. Don't build it with bool.ToString(), that gives "True", which isn't JSON.
        string json = JsonUtility.ToJson(new TssSwitchBody { value = value });
        StartCoroutine(Post($"/{panel}/{name}", json));
    }

    IEnumerator Post(string path, string json)
    {
        using (var req = new UnityWebRequest(HttpBase + path, "POST"))
        {
            req.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(json));
            req.downloadHandler = new DownloadHandlerBuffer();
            req.SetRequestHeader("Content-Type", "application/json");
            yield return req.SendWebRequest();
            if (req.result != UnityWebRequest.Result.Success)
                Debug.LogWarning($"[TSS] POST {path} failed: {req.error} {req.downloadHandler.text}");
        }
    }

    // ===== Debug panel =====
    // Editor only. OnGUI doesn't render on headsets, so real HMD UI should read Latest instead.
    void OnGUI()
    {
        if (!showDebugPanel) return;
        GUILayout.BeginArea(new Rect(10, 10, 290, Screen.height - 20), GUI.skin.box);
        if (Latest == null)
        {
            GUILayout.Label($"waiting for {HttpBase} ...");
            GUILayout.EndArea();
            return;
        }

        var s = Latest.suit;
        GUILayout.Label(Latest.eva_active ? $"EVA  {Latest.eva_time_s:F0} s" : "pre EVA");
        // Made up limits for practice. Real ones come from NASA's docs.
        Readout($"O2 PRI  {s.o2_pri_psi:F0} psi", s.o2_pri_psi < 500f);
        Readout($"O2 SEC  {s.o2_sec_psi:F0} psi", s.o2_sec_psi < 500f);
        Readout($"SUIT  {s.suit_pressure_psi:F2} psi", false);
        Readout($"BATT  {s.batt_pct:F0} %", s.batt_pct < 20f);
        Readout($"HR  {s.heart_rate_bpm:F0} bpm", s.heart_rate_bpm > 160f);
        Readout($"data age  {SecondsSinceUpdate:F1} s", SecondsSinceUpdate > 2f);

        GUILayout.Space(8);
        GUILayout.Label("UIA");
        SwitchRow("uia", "emu1_power", "EMU1 PWR", Latest.uia.emu1_power);
        SwitchRow("uia", "depress_pump", "DEPRESS PUMP PWR", Latest.uia.depress_pump);
        SwitchRow("uia", "o2_vent", "O2 VENT open", Latest.uia.o2_vent);
        SwitchRow("uia", "emu1_oxygen", "OXYGEN EMU1 open", Latest.uia.emu1_oxygen);

        GUILayout.Space(8);
        GUILayout.Label("DCU");
        SwitchRow("dcu", "batt_umb", "BATT  UMB (off = LOCAL)", Latest.dcu.batt_umb);
        SwitchRow("dcu", "oxy_pri", "OXY  PRI (off = SEC)", Latest.dcu.oxy_pri);
        SwitchRow("dcu", "comms_a", "COMMS  A (off = B)", Latest.dcu.comms_a);
        SwitchRow("dcu", "fan_pri", "FAN  PRI (off = SEC)", Latest.dcu.fan_pri);
        SwitchRow("dcu", "pump_open", "PUMP  OPEN (off = CLOSE)", Latest.dcu.pump_open);
        SwitchRow("dcu", "co2_a", "CO2  A (off = B)", Latest.dcu.co2_a);
        GUILayout.EndArea();
    }

    void Readout(string text, bool alarm)
    {
        var old = GUI.contentColor;
        if (alarm) GUI.contentColor = Color.red;
        GUILayout.Label(text);
        GUI.contentColor = old;
    }

    void SwitchRow(string panel, string name, string label, bool current)
    {
        // The server is the source of truth. We send the flip and let the next
        // telemetry message show it, instead of changing anything locally.
        if (GUILayout.Toggle(current, label) != current) SetSwitch(panel, name, !current);
    }
}
