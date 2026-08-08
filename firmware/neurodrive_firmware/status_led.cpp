#include "status_led.h"

#include "config.h"
#include "motor_control.h"
#include "safety.h"

#define BLINK_SLOW_MS 500
#define BLINK_FAST_MS 120

// Returns true for the "on" half of a blink cycle of the given period.
static bool blinkPhase(unsigned long now, unsigned long periodMs) {
  return (now / periodMs) % 2 == 0;
}

void ledSetup() {
  pinMode(PIN_LED_BUILTIN, OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_LED_YELLOW, OUTPUT);
  pinMode(PIN_LED_RED, OUTPUT);

  digitalWrite(PIN_LED_BUILTIN, LOW);
  digitalWrite(PIN_LED_GREEN, LOW);
  digitalWrite(PIN_LED_YELLOW, LOW);
  digitalWrite(PIN_LED_RED, HIGH);  // stopped is the boot state
}

void ledTick(unsigned long now, bool linkActive) {
  const MotorState state = motorGetState();

  bool green = (state == STATE_FORWARD);
  bool yellow = (state == STATE_TURN_LEFT || state == STATE_TURN_RIGHT);
  bool red = false;

  if (state == STATE_STOP) {
    switch (motorGetStopReason()) {
      case STOP_REASON_ESTOP:
        red = blinkPhase(now, BLINK_FAST_MS);
        break;
      case STOP_REASON_WATCHDOG:
        red = blinkPhase(now, BLINK_SLOW_MS);
        break;
      default:
        red = true;  // ordinary commanded stop
        break;
    }
  }

  digitalWrite(PIN_LED_GREEN, green ? HIGH : LOW);
  digitalWrite(PIN_LED_YELLOW, yellow ? HIGH : LOW);
  digitalWrite(PIN_LED_RED, red ? HIGH : LOW);

  // Heartbeat: solid once commands are flowing, slow blink while waiting.
  const bool heartbeat = linkActive ? true : blinkPhase(now, BLINK_SLOW_MS);
  digitalWrite(PIN_LED_BUILTIN, heartbeat ? HIGH : LOW);
}
