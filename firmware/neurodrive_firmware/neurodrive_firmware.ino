/*
 * NeuroDrive ESP32 vehicle firmware.
 *
 * Receives movement commands from the Python bridge (over WiFi UDP, or over
 * USB serial as a fallback) and drives a 2WD chassis through an L298N.
 *
 * Architecture: four modules, each with one job.
 *
 *     safety.*        emergency stop and watchdog
 *     comm.*          UDP + serial receive, ack
 *     motor_control.* state machine and PWM output
 *     status_led.*    indicator LEDs
 *
 * loop() runs them in that order deliberately: safety first, so a tripped
 * watchdog or a pressed button stops the vehicle before any newly arrived
 * command can be acted on.
 *
 * Pin assignment: config.h.
 *
 * Build: Arduino IDE (open this folder) or PlatformIO (`pio run -t upload`
 * from firmware/). See firmware/README.md.
 */

#include <Arduino.h>

#include "comm.h"
#include "config.h"
#include "motor_control.h"
#include "safety.h"
#include "status_led.h"

static unsigned long lastTelemetryMs = 0;
static MotorState lastReportedState = STATE_STOP;

static void printBanner() {
  Serial.println();
  Serial.println(F("========================================"));
  Serial.println(F(" NeuroDrive vehicle firmware"));
  Serial.println(F("========================================"));
  Serial.print(F(" motor speed   : "));
  Serial.print(SPEED_FORWARD_PCT);
  Serial.println(F("% forward"));
  Serial.print(F(" turn pulse    : "));
  Serial.print(TURN_PULSE_MS);
  Serial.println(F(" ms"));
  Serial.print(F(" watchdog      : "));
  Serial.print(WATCHDOG_TIMEOUT_MS);
  Serial.println(F(" ms"));
  Serial.print(F(" command port  : "));
  Serial.println(UDP_COMMAND_PORT);
  Serial.println(F("----------------------------------------"));
}

static void printTelemetry(unsigned long now) {
  Serial.print(F("[state] "));
  Serial.print(motorStateName(motorGetState()));
  Serial.print(F(" reason="));
  Serial.print(motorStopReasonName(motorGetStopReason()));
  Serial.print(F(" rx="));
  Serial.print(commPacketsReceived());
  Serial.print(F(" bad="));
  Serial.print(commPacketsRejected());
  Serial.print(F(" since_cmd="));
  Serial.print(safetyMillisSinceCommand(now));
  Serial.print(F("ms wifi="));
  Serial.print(commWifiConnected() ? commModeName() : "down");
  Serial.print(F(" ip="));
  Serial.print(commLocalIP());
  Serial.print(F(" estop="));
  Serial.print(safetyEstopActive() ? "PRESSED" : "clear");
  Serial.print(F(" turns="));
  Serial.println(motorTurnCount());
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  // Give the USB CDC / serial monitor a moment, but never block forever:
  // the vehicle must come up whether or not anyone is watching.
  const unsigned long serialDeadline = millis() + 1500;
  while (!Serial && millis() < serialDeadline) {
    delay(10);
  }

  // Motors first: this drives every L298N input low before anything else
  // can run, so the vehicle cannot twitch during boot.
  motorSetup();
  ledSetup();
  safetySetup();

  printBanner();
  commSetup();

  Serial.println(F("[boot] ready, waiting for commands"));
}

void loop() {
  const unsigned long now = millis();

  // 1. Safety supervision. Runs first and unconditionally.
  safetyTick(now);

  // 2. Receive and dispatch commands (non-blocking).
  commTick(now);

  // 3. Advance the motor state machine (expires turn pulses).
  motorTick(now);

  // 4. Indicators. "Link active" means a command arrived recently enough
  //    that the watchdog is satisfied.
  ledTick(now, !safetyWatchdogTripped());

  // 5. Telemetry for the serial monitor. Printed on every state change and
  //    then periodically, so the log shows both events and liveness.
  const MotorState state = motorGetState();
  if (state != lastReportedState) {
    lastReportedState = state;
    printTelemetry(now);
    lastTelemetryMs = now;
  }
#if TELEMETRY_INTERVAL_MS > 0
  else if ((now - lastTelemetryMs) >= TELEMETRY_INTERVAL_MS) {
    lastTelemetryMs = now;
    printTelemetry(now);
  }
#endif

  // No delay(): the loop never blocks, which keeps the command-to-motor
  // path under 10 ms. The 1 ms yield keeps the ESP32's WiFi and idle tasks
  // fed without adding real latency.
  delay(1);
}
