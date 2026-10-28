// Go port of mock_tss.py (Infra onboarding mock TSS).
// Same HTTP API and JSON shapes: /state /pois /uia/{switch} /dcu/{switch}
// /debug/suit/{field} /reset and a /ws WebSocket pushed TickHz times a second.
package main

import (
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"io"
	"log"
	"math"
	"math/rand"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	TickHz    = 5       // sim steps (and WebSocket pushes) per second
	WalkSpeed = 1.5     // meters per second
	DwellTime = 8.0     // seconds spent working at each POI
	Port      = ":8001" // Python mock uses 8000; this lets both run side by side
)

// r2 rounds to 2 decimals, like the Python Num serializer.
func r2(v float64) float64 { return math.Round(v*100) / 100 }

// ===== Data model (field names must match mock_tss.py exactly) =====

type Uia struct {
	Emu1Power   bool `json:"emu1_power"`
	DepressPump bool `json:"depress_pump"`
	O2Vent      bool `json:"o2_vent"`
	Emu1Oxygen  bool `json:"emu1_oxygen"`
}

type Dcu struct {
	BattUmb  bool `json:"batt_umb"`  // true = UMB, false = LOCAL
	OxyPri   bool `json:"oxy_pri"`   // true = PRI, false = SEC
	CommsA   bool `json:"comms_a"`   // true = A,   false = B
	FanPri   bool `json:"fan_pri"`   // true = PRI, false = SEC
	PumpOpen bool `json:"pump_open"` // true = OPEN
	Co2A     bool `json:"co2_a"`     // true = A,   false = B
}

type Suit struct {
	O2PriPsi        float64 `json:"o2_pri_psi"`
	O2SecPsi        float64 `json:"o2_sec_psi"`
	SuitPressurePsi float64 `json:"suit_pressure_psi"`
	BattPct         float64 `json:"batt_pct"`
	HeartRateBpm    float64 `json:"heart_rate_bpm"`
}

type Imu struct {
	X       float64 `json:"x"`       // meters east of the lander
	Y       float64 `json:"y"`       // meters north of the lander
	Heading float64 `json:"heading"` // degrees, 0 = north, 90 = east
}

type State struct {
	EvaActive bool    `json:"eva_active"`
	EvaTimeS  float64 `json:"eva_time_s"`
	Uia       Uia     `json:"uia"`
	Dcu       Dcu     `json:"dcu"`
	Suit      Suit    `json:"suit"`
	Imu       Imu     `json:"imu"`
}

// Output rounding: apply r2 when serializing, keep full precision internally.
func (s Suit) MarshalJSON() ([]byte, error) {
	type plain Suit
	p := plain{r2(s.O2PriPsi), r2(s.O2SecPsi), r2(s.SuitPressurePsi), r2(s.BattPct), r2(s.HeartRateBpm)}
	return json.Marshal(p)
}

func (m Imu) MarshalJSON() ([]byte, error) {
	type plain Imu
	return json.Marshal(plain{r2(m.X), r2(m.Y), r2(m.Heading)})
}

func (s State) MarshalJSON() ([]byte, error) {
	type plain State
	p := plain(s)
	p.EvaTimeS = r2(p.EvaTimeS)
	return json.Marshal(p)
}

func newState() State {
	return State{
		Dcu:  Dcu{OxyPri: true, CommsA: true, FanPri: true, Co2A: true},
		Suit: Suit{O2PriPsi: 800.0, O2SecPsi: 750.0, SuitPressurePsi: 14.7, BattPct: 100.0, HeartRateBpm: 75.0},
	}
}

type Poi struct {
	Name string  `json:"name"`
	X    float64 `json:"x"`
	Y    float64 `json:"y"`
}

// POIs sit in a loop around the lander, like the real scenario.
var pois = []Poi{
	{"lander", 0.0, 0.0},
	{"A", 20.0, 25.0},
	{"B", -15.0, 30.0},
	{"C", -25.0, 5.0},
}

var (
	mu        sync.Mutex
	state     = newState()
	nextPoi   = 1
	dwellLeft = 0.0
	clients   = map[*client]struct{}{}
)

// ===== Simulation =====

func step(dt float64) {
	u, d, s := &state.Uia, &state.Dcu, &state.Suit

	// O2 VENT open: both tanks bleed down to 0 psi
	if u.O2Vent {
		s.O2PriPsi = math.Max(0, s.O2PriPsi-(0.4*s.O2PriPsi+5)*dt)
		s.O2SecPsi = math.Max(0, s.O2SecPsi-(0.4*s.O2SecPsi+5)*dt)
	} else if u.Emu1Oxygen && u.Emu1Power {
		// OXYGEN EMU1 open with power on: fill whichever tank the DCU selected
		if d.OxyPri {
			s.O2PriPsi = math.Min(3200, s.O2PriPsi+250*dt)
		} else {
			s.O2SecPsi = math.Min(3200, s.O2SecPsi+250*dt)
		}
	}

	// Breathing pulls O2 from the selected tank. Higher heart rate, more O2.
	breath := 0.02 * s.HeartRateBpm * dt
	if d.OxyPri {
		s.O2PriPsi = math.Max(0, s.O2PriPsi-breath)
	} else {
		s.O2SecPsi = math.Max(0, s.O2SecPsi-breath)
	}

	// Depress pump (needs EMU power) brings the suit down to 4 psi
	if u.DepressPump && u.Emu1Power {
		s.SuitPressurePsi = math.Max(4.0, s.SuitPressurePsi-0.35*dt)
	}

	// Umbilical power charges the battery, local battery drains it
	if d.BattUmb && u.Emu1Power {
		s.BattPct = math.Min(100, s.BattPct+2.0*dt)
	} else {
		s.BattPct = math.Max(0, s.BattPct-0.05*dt)
	}

	// The EVA starts once egress is done: EMU power off, local battery, suit at 4 psi
	state.EvaActive = !u.Emu1Power && !d.BattUmb && s.SuitPressurePsi <= 4.05

	// Heart rate drifts toward a resting or working target, plus some noise
	target := 75.0
	if state.EvaActive {
		target = 100.0
	}
	s.HeartRateBpm += (target - s.HeartRateBpm) * 0.02 * dt
	s.HeartRateBpm += rand.NormFloat64() * 1.0 * math.Sqrt(dt)
	s.HeartRateBpm = math.Min(190, math.Max(45, s.HeartRateBpm))

	if state.EvaActive {
		state.EvaTimeS += dt
		walk(dt)
	}
}

// walk moves the EV around the POI loop: lander, A, B, C, lander, and so on.
func walk(dt float64) {
	if dwellLeft > 0 { // still working at a POI
		dwellLeft -= dt
		return
	}
	m, goal := &state.Imu, pois[nextPoi]
	dx, dy := goal.X-m.X, goal.Y-m.Y
	dist := math.Hypot(dx, dy)
	if dist < 0.5 { // arrived: work for a bit, then head to the next one
		nextPoi = (nextPoi + 1) % len(pois)
		dwellLeft = DwellTime
		return
	}
	stride := math.Min(dist, WalkSpeed*dt)
	m.X += dx / dist * stride
	m.Y += dy / dist * stride
	h := math.Mod(math.Atan2(dx, dy)*180/math.Pi, 360)
	if h < 0 {
		h += 360 // match Python's % (result takes the sign of the divisor)
	}
	m.Heading = h
}

// ===== WebSocket (stdlib only: server -> client text frames) =====
// Minimal RFC 6455: handshake, unfragmented text frames out, incoming frames
// are read and ignored (like mock_tss.py), a read error means "client left".

type client struct {
	conn net.Conn
	send chan []byte
}

func textFrame(payload []byte) []byte {
	n := len(payload)
	var hdr []byte
	switch {
	case n < 126:
		hdr = []byte{0x81, byte(n)}
	case n < 65536:
		hdr = []byte{0x81, 126, 0, 0}
		binary.BigEndian.PutUint16(hdr[2:], uint16(n))
	default:
		hdr = make([]byte, 10)
		hdr[0], hdr[1] = 0x81, 127
		binary.BigEndian.PutUint64(hdr[2:], uint64(n))
	}
	return append(hdr, payload...)
}

func (c *client) writeLoop() {
	defer c.conn.Close()
	for msg := range c.send {
		c.conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
		if _, err := c.conn.Write(textFrame(msg)); err != nil {
			return
		}
	}
}

func simLoop() {
	dt := 1.0 / TickHz
	ticker := time.NewTicker(time.Second / TickHz)
	defer ticker.Stop()
	for range ticker.C {
		mu.Lock()
		step(dt)
		payload, _ := json.Marshal(state)
		for c := range clients {
			select {
			case c.send <- payload:
			default: // slow client: drop this frame rather than block the sim
			}
		}
		mu.Unlock()
	}
}

const wsGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

func handleWS(w http.ResponseWriter, r *http.Request) {
	key := r.Header.Get("Sec-WebSocket-Key")
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") || key == "" {
		http.Error(w, "websocket upgrade required", http.StatusBadRequest)
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "cannot hijack", http.StatusInternalServerError)
		return
	}
	conn, rw, err := hj.Hijack()
	if err != nil {
		return
	}
	sum := sha1.Sum([]byte(key + wsGUID))
	accept := base64.StdEncoding.EncodeToString(sum[:])
	rw.WriteString("HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + accept + "\r\n\r\n")
	if rw.Flush() != nil {
		conn.Close()
		return
	}

	c := &client{conn: conn, send: make(chan []byte, 8)}
	mu.Lock()
	clients[c] = struct{}{}
	mu.Unlock()
	go c.writeLoop()

	// Incoming frames are ignored; reading just detects a disconnect.
	io.Copy(io.Discard, rw.Reader)

	mu.Lock()
	delete(clients, c)
	mu.Unlock()
	close(c.send)
}

// ===== HTTP =====

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		http.Error(w, `{"detail":"invalid body"}`, http.StatusUnprocessableEntity)
		return false
	}
	return true
}

// switches maps a switch name to the field it controls (call with mu held).
func uiaFields() map[string]*bool {
	u := &state.Uia
	return map[string]*bool{"emu1_power": &u.Emu1Power, "depress_pump": &u.DepressPump, "o2_vent": &u.O2Vent, "emu1_oxygen": &u.Emu1Oxygen}
}

func dcuFields() map[string]*bool {
	d := &state.Dcu
	return map[string]*bool{"batt_umb": &d.BattUmb, "oxy_pri": &d.OxyPri, "comms_a": &d.CommsA, "fan_pri": &d.FanPri, "pump_open": &d.PumpOpen, "co2_a": &d.Co2A}
}

func suitFields() map[string]*float64 {
	s := &state.Suit
	return map[string]*float64{"o2_pri_psi": &s.O2PriPsi, "o2_sec_psi": &s.O2SecPsi, "suit_pressure_psi": &s.SuitPressurePsi, "batt_pct": &s.BattPct, "heart_rate_bpm": &s.HeartRateBpm}
}

func setBool(fields func() map[string]*bool, group func() any) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Value *bool `json:"value"`
		}
		if !decode(w, r, &body) {
			return
		}
		if body.Value == nil {
			http.Error(w, `{"detail":"value required"}`, http.StatusUnprocessableEntity)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		p, ok := fields()[r.PathValue("switch")]
		if !ok {
			http.Error(w, `{"detail":"unknown switch"}`, http.StatusUnprocessableEntity)
			return
		}
		*p = *body.Value
		writeJSON(w, group()) // like Python: reply with the whole group
	}
}

func lanIP() string {
	c, err := net.Dial("udp", "8.8.8.8:80") // UDP dial sends nothing, it just picks a route
	if err != nil {
		return "127.0.0.1"
	}
	defer c.Close()
	return c.LocalAddr().(*net.UDPAddr).IP.String()
}

func main() {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /state", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		writeJSON(w, state)
	})
	mux.HandleFunc("GET /pois", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"pois": pois})
	})
	mux.HandleFunc("POST /uia/{switch}", setBool(uiaFields, func() any { return state.Uia }))
	mux.HandleFunc("POST /dcu/{switch}", setBool(dcuFields, func() any { return state.Dcu }))
	mux.HandleFunc("POST /debug/suit/{field}", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Value *float64 `json:"value"`
		}
		if !decode(w, r, &body) {
			return
		}
		if body.Value == nil {
			http.Error(w, `{"detail":"value required"}`, http.StatusUnprocessableEntity)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		p, ok := suitFields()[r.PathValue("field")]
		if !ok {
			http.Error(w, `{"detail":"unknown field"}`, http.StatusUnprocessableEntity)
			return
		}
		*p = *body.Value
		writeJSON(w, state.Suit)
	})
	mux.HandleFunc("POST /reset", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		state, nextPoi, dwellLeft = newState(), 1, 0.0
		writeJSON(w, state)
	})
	mux.HandleFunc("GET /ws", handleWS)

	go simLoop()

	log.Printf("Go mock TSS: http://localhost%s/state  ws://localhost%s/ws", Port, Port)
	log.Printf("headset: set TssClient host to %s, port %s", lanIP(), Port[1:])
	log.Fatal(http.ListenAndServe(Port, mux))
}
