#include "safety.h"

#include "config.h"
#include "motor_control.h"

// --- Emergency stop (SF-01) -------------------------------------------------

static bool estopActive = false;         // debounced, latched state
static int estopLastRawReading = HIGH;   // INPUT_PULLUP: HIGH = released
static unsigned long estopLastChangeMs = 0;
static unsigned long estopReleasedAtMs = 0;
static unsigned long estopPressCount = 0;

// --- Watchdog (SF-02) -------------------------------------------------------

static unsigned long lastCommandMs = 0;
static bool watchdogTripped = false;
static unsigned long watchdogTripCount = 0;

void safetySetup() {
  pinMode(PIN_ESTOP, INPUT_PULLUP);
  estopLastRawReading = digitalRead(PIN_ESTOP);
  estopActive = (estopLastRawReading == LOW);
  estopLastChangeMs = millis();
  estopReleasedAtMs = millis();

  // Start already tripped: the vehicle must not move until the bridge has
  // actually said something. Booting into "waiting for a command" is safer
  // than booting into a 2-second grace period. Backdating the timestamp
  // relies on unsigned wraparound, which is exactly how the comparison in
  // tickWatchdog() is written, so this is well-defined even at millis()==0.
  lastCommandMs = millis() - WATCHDOG_TIMEOUT_MS;
  watchdogTripped = true;  // pre-set, so the boot state is not counted as a trip

  if (estopActive) {
    motorEmergencyStop(STOP_REASON_ESTOP);
  }
}

static void tickEstop(unsigned long now) {
  const int reading = digitalRead(PIN_ESTOP);

  if (reading != estopLastRawReading) {
    estopLastRawReading = reading;
    estopLastChangeMs = now;
    return;  // wait for the contacts to settle
  }

  if ((now - estopLastChangeMs) < ESTOP_DEBOUNCE_MS) {
    return;
  }

  const bool pressed = (reading == LOW);

  if (pressed && !estopActive) {
    estopActive = true;
    estopPressCount++;
    motorEmergencyStop(STOP_REASON_ESTOP);
    return;
  }

  if (!pressed && estopActive) {
    estopActive = false;
    estopReleasedAtMs = now;
    return;
  }

  // Released and settled for long enough: allow movement commands again.
  // The vehicle still stays stopped until a new command arrives.
  if (!pressed && motorIsLatched() && (now - estopReleasedAtMs) >= ESTOP_RELEASE_MS) {
    motorClearEmergencyStop();
  }
}

static void tickWatchdog(unsigned long now) {
  if (estopActive) {
    return;  // the e-stop already owns the motors
  }

  // Unsigned subtraction, so this stays correct across the millis() rollover
  // at ~49 days.
  if ((now - lastCommandMs) >= WATCHDOG_TIMEOUT_MS) {
    if (!watchdogTripped) {
      watchdogTripped = true;
      watchdogTripCount++;
    }
    if (motorGetState() != STATE_STOP || motorGetStopReason() != STOP_REASON_WATCHDOG) {
      motorEmergencyStop(STOP_REASON_WATCHDOG);
    }
  }
}

void safetyTick(unsigned long now) {
  tickEstop(now);
  tickWatchdog(now);
}

void safetyFeedWatchdog(unsigned long now) {
  lastCommandMs = now;
  watchdogTripped = false;
}

bool safetyEstopActive() { return estopActive; }
bool safetyWatchdogTripped() { return watchdogTripped; }

unsigned long safetyMillisSinceCommand(unsigned long now) {
  return now - lastCommandMs;
}

unsigned long safetyWatchdogTripCount() { return watchdogTripCount; }
unsigned long safetyEstopPressCount() { return estopPressCount; }
