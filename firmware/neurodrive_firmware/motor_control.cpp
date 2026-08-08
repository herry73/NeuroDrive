#include "motor_control.h"

#include "config.h"

// ---------------------------------------------------------------------------
// LEDC compatibility
//
// The ESP32 Arduino core changed its PWM API in 3.0: channels disappeared and
// ledcAttach()/ledcWrite() now take the pin directly. Supporting both means
// the team can use whichever core version the Arduino IDE installs.
// ---------------------------------------------------------------------------

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
#define PWM_ATTACH(pin, channel) ledcAttach((pin), PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS)
#define PWM_WRITE(pin, channel, duty) ledcWrite((pin), (duty))
#else
#define PWM_ATTACH(pin, channel)                                            \
  do {                                                                      \
    ledcSetup((channel), PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS);            \
    ledcAttachPin((pin), (channel));                                        \
  } while (0)
#define PWM_WRITE(pin, channel, duty) ledcWrite((channel), (duty))
#endif

#define PWM_CHANNEL_LEFT 0
#define PWM_CHANNEL_RIGHT 1

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

static MotorState currentState = STATE_STOP;
static MotorState baseState = STATE_STOP;   // resumes when a turn pulse ends
static MotorState appliedState = STATE_STOP;
static StopReason stopReason = STOP_REASON_NONE;
static bool estopLatched = false;
static unsigned long turnStartedAt = 0;
static unsigned long transitionCount = 0;
static unsigned long turnCount = 0;
static bool outputsValid = false;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Convert a percentage of full scale into a duty value, applying that
// side's trim and refusing to emit a duty too small to actually move.
static int dutyFor(int percent, int trimPercent) {
  long duty = ((long)PWM_MAX_DUTY * percent * trimPercent) / 10000L;
  if (duty <= 0) {
    return 0;
  }
  if (duty < MIN_MOVE_DUTY) {
    duty = MIN_MOVE_DUTY;
  }
  if (duty > PWM_MAX_DUTY) {
    duty = PWM_MAX_DUTY;
  }
  return (int)duty;
}

// direction: +1 forward, -1 reverse, 0 coast.
static void driveSide(int in1, int in2, int pwmPin, int pwmChannel, int direction,
                      int duty) {
  if (direction == 0 || duty == 0) {
    // Both inputs low = fast stop on the L298N, and zero enable so the
    // bridge is not left half-driven.
    digitalWrite(in1, LOW);
    digitalWrite(in2, LOW);
    PWM_WRITE(pwmPin, pwmChannel, 0);
    return;
  }
  digitalWrite(in1, direction > 0 ? HIGH : LOW);
  digitalWrite(in2, direction > 0 ? LOW : HIGH);
  PWM_WRITE(pwmPin, pwmChannel, duty);
}

static void applyOutputs(bool force) {
  if (!force && outputsValid && appliedState == currentState) {
    return;
  }

  const int forwardDuty = SPEED_FORWARD_PCT;
  const int turnDuty = SPEED_TURN_PCT;

  switch (currentState) {
    case STATE_FORWARD:
      driveSide(PIN_LEFT_IN1, PIN_LEFT_IN2, PIN_LEFT_ENA, PWM_CHANNEL_LEFT, +1,
                dutyFor(forwardDuty, TRIM_LEFT_PCT));
      driveSide(PIN_RIGHT_IN3, PIN_RIGHT_IN4, PIN_RIGHT_ENB, PWM_CHANNEL_RIGHT, +1,
                dutyFor(forwardDuty, TRIM_RIGHT_PCT));
      break;

    case STATE_TURN_LEFT:
#if TURN_STYLE_PIVOT
      // Counter-rotate: tightest turn, works on any surface.
      driveSide(PIN_LEFT_IN1, PIN_LEFT_IN2, PIN_LEFT_ENA, PWM_CHANNEL_LEFT, -1,
                dutyFor(turnDuty, TRIM_LEFT_PCT));
#else
      // Differential: inside wheel stopped, so the vehicle arcs forward.
      driveSide(PIN_LEFT_IN1, PIN_LEFT_IN2, PIN_LEFT_ENA, PWM_CHANNEL_LEFT, 0, 0);
#endif
      driveSide(PIN_RIGHT_IN3, PIN_RIGHT_IN4, PIN_RIGHT_ENB, PWM_CHANNEL_RIGHT, +1,
                dutyFor(turnDuty, TRIM_RIGHT_PCT));
      break;

    case STATE_TURN_RIGHT:
      driveSide(PIN_LEFT_IN1, PIN_LEFT_IN2, PIN_LEFT_ENA, PWM_CHANNEL_LEFT, +1,
                dutyFor(turnDuty, TRIM_LEFT_PCT));
#if TURN_STYLE_PIVOT
      driveSide(PIN_RIGHT_IN3, PIN_RIGHT_IN4, PIN_RIGHT_ENB, PWM_CHANNEL_RIGHT, -1,
                dutyFor(turnDuty, TRIM_RIGHT_PCT));
#else
      driveSide(PIN_RIGHT_IN3, PIN_RIGHT_IN4, PIN_RIGHT_ENB, PWM_CHANNEL_RIGHT, 0, 0);
#endif
      break;

    case STATE_STOP:
    default:
      driveSide(PIN_LEFT_IN1, PIN_LEFT_IN2, PIN_LEFT_ENA, PWM_CHANNEL_LEFT, 0, 0);
      driveSide(PIN_RIGHT_IN3, PIN_RIGHT_IN4, PIN_RIGHT_ENB, PWM_CHANNEL_RIGHT, 0, 0);
      break;
  }

  appliedState = currentState;
  outputsValid = true;
}

static void enterState(MotorState next) {
  if (next != currentState) {
    transitionCount++;
  }
  currentState = next;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void motorSetup() {
  pinMode(PIN_LEFT_IN1, OUTPUT);
  pinMode(PIN_LEFT_IN2, OUTPUT);
  pinMode(PIN_RIGHT_IN3, OUTPUT);
  pinMode(PIN_RIGHT_IN4, OUTPUT);

  PWM_ATTACH(PIN_LEFT_ENA, PWM_CHANNEL_LEFT);
  PWM_ATTACH(PIN_RIGHT_ENB, PWM_CHANNEL_RIGHT);

  currentState = STATE_STOP;
  baseState = STATE_STOP;
  stopReason = STOP_REASON_NONE;
  estopLatched = false;
  // Force the outputs low before anything else runs: the vehicle must never
  // twitch during boot.
  applyOutputs(true);
}

void motorSetState(MotorState state) {
  if (estopLatched && state != STATE_STOP) {
    // SF-01: while the button is held, movement requests are refused.
    return;
  }

  switch (state) {
    case STATE_STOP:
      baseState = STATE_STOP;
      turnStartedAt = 0;
      // An explicit STOP from the host clears a WATCHDOG reason -- otherwise
      // the red LED would keep blinking "no commands" after the link came
      // back. A latched e-stop keeps its reason: the button, not the host,
      // decides when that clears.
      if (!estopLatched) {
        stopReason = STOP_REASON_COMMAND;
      }
      enterState(STATE_STOP);
      break;

    case STATE_FORWARD:
      baseState = STATE_FORWARD;
      stopReason = STOP_REASON_NONE;
      if (currentState != STATE_TURN_LEFT && currentState != STATE_TURN_RIGHT) {
        // Mid-turn FORWARD only updates what resumes afterwards (MV-03).
        enterState(STATE_FORWARD);
      }
      break;

    case STATE_TURN_LEFT:
    case STATE_TURN_RIGHT:
      stopReason = STOP_REASON_NONE;
      if (currentState == state) {
        // Already pulsing this way: do not restart the timer, otherwise the
        // bridge's keepalive re-sends would extend the turn forever.
        break;
      }
      turnStartedAt = millis();
      turnCount++;
      enterState(state);
      break;
  }

  // MV-04: act now rather than waiting for the next tick.
  applyOutputs(false);
}

void motorTick(unsigned long now) {
  if ((currentState == STATE_TURN_LEFT || currentState == STATE_TURN_RIGHT) &&
      (now - turnStartedAt) >= TURN_PULSE_MS) {
    enterState(estopLatched ? STATE_STOP : baseState);
  }
  applyOutputs(false);
}

void motorEmergencyStop(StopReason reason) {
  estopLatched = (reason == STOP_REASON_ESTOP);
  baseState = STATE_STOP;
  turnStartedAt = 0;
  stopReason = reason;
  enterState(STATE_STOP);
  applyOutputs(true);
}

void motorClearEmergencyStop() {
  estopLatched = false;
  if (stopReason == STOP_REASON_ESTOP) {
    stopReason = STOP_REASON_COMMAND;
  }
}

MotorState motorGetState() { return currentState; }
MotorState motorGetBaseState() { return baseState; }
StopReason motorGetStopReason() { return stopReason; }
bool motorIsLatched() { return estopLatched; }
unsigned long motorTransitionCount() { return transitionCount; }
unsigned long motorTurnCount() { return turnCount; }

const char* motorStateName(MotorState state) {
  switch (state) {
    case STATE_FORWARD: return "FORWARD";
    case STATE_TURN_LEFT: return "TURN_LEFT";
    case STATE_TURN_RIGHT: return "TURN_RIGHT";
    case STATE_STOP: return "STOP";
    default: return "?";
  }
}

const char* motorStopReasonName(StopReason reason) {
  switch (reason) {
    case STOP_REASON_COMMAND: return "COMMAND";
    case STOP_REASON_WATCHDOG: return "WATCHDOG";
    case STOP_REASON_ESTOP: return "ESTOP";
    case STOP_REASON_NONE: return "NONE";
    default: return "?";
  }
}
