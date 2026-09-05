/*
 * Motor control state machine.
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
  STOP_REASON_WATCHDOG,   //: no command for WATCHDOG_TIMEOUT_MS
  STOP_REASON_ESTOP       //: hardware button pressed
};

void motorSetup();

// Ask for a state change. Ignored, except for STOP, while the emergency
// stop is latched. Asking again for the turn already running does NOT
// restart its timer, so keepalive re-sends cannot stretch a 300 ms turn
// forever.
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
