/*
 * Safety supervision: watchdog and emergency stop.
 *
 * Requirement coverage:
 *   SF-01  Hardware emergency stop button on the chassis.
 *   SF-02  Watchdog: no valid command for 2 s stops the vehicle.
 *
 * These checks run before anything else in loop(), and they call
 * motorEmergencyStop() directly. They never depend on the network stack,
 * so a hung WiFi task cannot prevent the vehicle from stopping.
 *
 * NOTE FOR M4: the button wired to PIN_ESTOP is the *signalling* half of
 * the emergency stop. The plan (SF-01) also requires it to physically
 * interrupt the motor supply. Wire both: firmware can fail, a switch in the
 * battery line cannot.
 */

#ifndef NEURODRIVE_SAFETY_H
#define NEURODRIVE_SAFETY_H

#include <Arduino.h>

void safetySetup();

// Call once per loop iteration, before processing any commands.
void safetyTick(unsigned long now);

// Called by the communication layer whenever a valid command is accepted.
void safetyFeedWatchdog(unsigned long now);

bool safetyEstopActive();
bool safetyWatchdogTripped();
unsigned long safetyMillisSinceCommand(unsigned long now);
unsigned long safetyWatchdogTripCount();
unsigned long safetyEstopPressCount();

#endif  // NEURODRIVE_SAFETY_H
