/*
 * Status indicators.
 *
 * LEDs on the vehicle, so you can see what it is doing without looking at
 * the laptop.
 *
 *   Green  (GPIO 19)  driving forward
 *   Yellow (GPIO 18)  turning
 *   Red    (GPIO 5)   stopped. Solid on command, blinking on watchdog,
 *                     fast blink when the emergency stop is latched
 *   Built-in (GPIO 2) link heartbeat: solid when the host is talking to us,
 *                     slow blink while waiting for the first command
 *
 * Every LED is optional. Leave them unwired if you like; nothing else
 * depends on this module.
 */

#ifndef NEURODRIVE_STATUS_LED_H
#define NEURODRIVE_STATUS_LED_H

#include <Arduino.h>

void ledSetup();

// Call once per loop iteration. Reads the motor and safety modules itself,
// so it never needs to be told what changed.
void ledTick(unsigned long now, bool linkActive);

#endif  // NEURODRIVE_STATUS_LED_H
