/*
 * NeuroDrive firmware build-time configuration.
 *
 * Everything tunable lives here. Check the pin numbers against your own
 * ESP32 board variant before wiring.
 *
 * WiFi credentials do NOT live here. Copy secrets.h.example to secrets.h
 * and edit that. secrets.h is git-ignored so the demo hotspot password is
 * never committed.
 */

#ifndef NEURODRIVE_CONFIG_H
#define NEURODRIVE_CONFIG_H

// ---------------------------------------------------------------------------
// Pin assignment
// ---------------------------------------------------------------------------

// L298N direction inputs
#define PIN_LEFT_IN1 25   // Left motor direction A
#define PIN_LEFT_IN2 26   // Left motor direction B
#define PIN_RIGHT_IN3 27  // Right motor direction A
#define PIN_RIGHT_IN4 14  // Right motor direction B

// L298N enable pins (PWM speed control)
#define PIN_LEFT_ENA 32
#define PIN_RIGHT_ENB 33

// Emergency stop button: INPUT_PULLUP, pressed = LOW.
// This is the *signalling* path only. The button must ALSO physically break
// the motor supply. Firmware alone is not an emergency stop.
#define PIN_ESTOP 4

// Status indicators
#define PIN_LED_BUILTIN 2   // heartbeat / link status
#define PIN_LED_GREEN 19    // forward
#define PIN_LED_YELLOW 18   // turning
#define PIN_LED_RED 5       // stopped

// ---------------------------------------------------------------------------
// Motor characteristics
// ---------------------------------------------------------------------------

#define PWM_FREQUENCY_HZ 1000
#define PWM_RESOLUTION_BITS 8
#define PWM_MAX_DUTY ((1 << PWM_RESOLUTION_BITS) - 1)  // 255

// Fixed safe speed, as a percentage of full scale.
#define SPEED_FORWARD_PCT 50
#define SPEED_TURN_PCT 55  // pivot turns need slightly more torque to break static friction

// Straight-line trim. Two "identical" motors never match, so scale each
// side until the vehicle drives straight.
#define TRIM_LEFT_PCT 100
#define TRIM_RIGHT_PCT 100

// Motors below roughly 30% duty usually stall and buzz instead of turning.
#define MIN_MOVE_DUTY 60

// Turning strategy.
//   1 = pivot turn: wheels counter-rotate. Tightest turn, works anywhere.
//   0 = differential turn: inside wheel stops, the vehicle arcs forward.
#define TURN_STYLE_PIVOT 1

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------

// a turn is a timed pulse, then the previous state resumes.
#define TURN_PULSE_MS 300

// stop if no valid command arrives within this window.
#define WATCHDOG_TIMEOUT_MS 2000

// Button debounce (mechanical contacts bounce for a few milliseconds).
#define ESTOP_DEBOUNCE_MS 30

// How long the e-stop must be released before driving is allowed again.
#define ESTOP_RELEASE_MS 500

// Serial link to the host (both for debugging and for the USB fallback).
#define SERIAL_BAUD 115200

// Status line printed to the serial monitor; 0 disables it.
#define TELEMETRY_INTERVAL_MS 1000

// ---------------------------------------------------------------------------
// Networking
// ---------------------------------------------------------------------------

// UDP command port.
#define UDP_COMMAND_PORT 4210

// Where acknowledgements go. 0 = reply to the sender's own source port,
// which is what the Python bridge expects.
#define UDP_ACK_PORT 0

// Set to 1 to run the ESP32 as its own access point. Most reliable
// option: no venue WiFi, no DHCP surprises, and the vehicle is always at
// 192.168.4.1.
//
// Do not rename this to WIFI_MODE_AP or WIFI_AP. Both names already exist
// in the ESP-IDF headers, and a macro using them breaks the build.
#define WIFI_USE_AP 1

// 2.4 GHz channel for the access point. The default of 1 is the busiest
// channel in most buildings, and the ESP32's small antenna loses to the
// stronger APs there, so the network never shows up in the laptop's list.
// Use 1, 6 or 11 (the non-overlapping ones), whichever is quietest.
#define WIFI_AP_CHANNEL 6

// Station-mode static IP. Leave WIFI_USE_STATIC_IP at 0 for DHCP; if you
// enable it, make sure the address is outside your router's DHCP pool.
#define WIFI_USE_STATIC_IP 0
#define WIFI_STATIC_IP 192, 168, 1, 100
#define WIFI_STATIC_GATEWAY 192, 168, 1, 1
#define WIFI_STATIC_SUBNET 255, 255, 255, 0

// Seconds to wait for a station-mode association before giving up and
// falling back to serial-only control.
#define WIFI_CONNECT_TIMEOUT_S 20

// ---------------------------------------------------------------------------
// Protocol
// ---------------------------------------------------------------------------

#define CMD_CHAR_FORWARD 'F'
#define CMD_CHAR_LEFT 'L'
#define CMD_CHAR_RIGHT 'R'
#define CMD_CHAR_STOP 'S'
#define CMD_CHAR_PING 'P'  // watchdog keepalive; does not change the state

// acknowledge each accepted command.
#define SEND_ACK 1

#define UDP_RX_BUFFER 64

#endif  // NEURODRIVE_CONFIG_H
