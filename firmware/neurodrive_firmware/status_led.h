/*
 * Status indicators.
 *
 * Should Have (plan section 10.2): LED status indicators on the vehicle so
 * the audience can see what the system is doing without reading the laptop.
 *
 *   Green  (GPIO 19)  driving forward
 *   Yellow (GPIO 18)  turning
 *   Red    (GPIO 5)   stopped. Solid on command, blinking on watchdog,
 *                     fast blink when the emergency stop is latched
 *   Built-in (GPIO 2) link heartbeat: solid when the host is talking to us,
 *                     slow blink while waiting for the first command
 *
 * Every LED is optional. If the team runs out of time or GPIO, leave them
 * unwired; nothing else depends on this module.
 */

#ifndef NEURODRIVE_STATUS_LED_H
#define NEURODRIVE_STATUS_LED_H

#include <Arduino.h>

void ledSetup();

// Call once per loop iteration. Reads the motor and safety modules itself,
// so it never needs to be told what changed.
void ledTick(unsigned long now, bool linkActive);

#endif  // NEURODRIVE_STATUS_LED_H
