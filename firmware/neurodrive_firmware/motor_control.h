/*
 * Motor control state machine.
 *
 * Requirement coverage:
 *   MV-01  Four states: STOP, FORWARD, TURN_LEFT, TURN_RIGHT.
 *   MV-02  L298N driven with hardware PWM on ENA/ENB.
 *   MV-03  Turns are a timed pulse; the previous state resumes afterwards.
 *   MV-04  STOP takes effect on the same tick it is requested.
 *   MV-05  Fixed demo speed from config.h.
 *   NFR 3.1 A state machine (not a pile of if/else) so no undefined state
 *           can be reached.
 *
 * The module owns the GPIO. Nothing else in the firmware writes to the
 * L298N pins, which is what makes the emergency stop and the watchdog
 * trustworthy: they only have to call one function.
 */

#ifndef NEURODRIVE_MOTOR_CONTROL_H
#define NEURODRIVE_MOTOR_CONTROL_H

#include <Arduino.h>

enum MotorState {
  STATE_STOP = 0,
  STATE_FORWARD,
  STATE_TURN_LEFT,
  STATE_TURN_RIGHT
};

// Why the vehicle is in its current state. Reported over serial, and used
// by the LED module. Ordered by severity; the highest one wins the LEDs.
enum StopReason {
  STOP_REASON_NONE = 0,
  STOP_REASON_COMMAND,    // the host asked for STOP
  STOP_REASON_WATCHDOG,   // SF-02: no command for WATCHDOG_TIMEOUT_MS
  STOP_REASON_ESTOP       // SF-01: hardware button pressed
};

void motorSetup();

// Request a state transition. Ignored (except for STOP) while the emergency
// stop is latched. Re-requesting the turn already in progress does NOT
// restart its timer, so the keepalive re-sends from the bridge cannot
// stretch a 300 ms turn indefinitely.
void motorSetState(MotorState state);

// Must be called every loop iteration: expires the turn pulse and applies
// the resulting outputs.
void motorTick(unsigned long now);

// Immediate, unconditional stop. Safe to call from anywhere.
void motorEmergencyStop(StopReason reason);

// Clear a latched emergency stop. The vehicle stays stopped until a new
// movement command arrives.
void motorClearEmergencyStop();

MotorState motorGetState();
MotorState motorGetBaseState();   // what resumes when a turn pulse ends
StopReason motorGetStopReason();
bool motorIsLatched();

// Human-readable names for telemetry.
const char* motorStateName(MotorState state);
const char* motorStopReasonName(StopReason reason);

// Counters for the serial telemetry line and the test report.
unsigned long motorTransitionCount();
unsigned long motorTurnCount();

#endif  // NEURODRIVE_MOTOR_CONTROL_H
