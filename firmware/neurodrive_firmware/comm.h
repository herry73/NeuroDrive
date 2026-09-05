/*
 * Communication: WiFi/UDP command receiver plus USB serial fallback.
 *
 * One ASCII character followed by a newline. The long forms ("FORWARD",
 * "STOP", ...) also work, so you can drive the vehicle by hand from a plain
 * serial monitor.
 */

#ifndef NEURODRIVE_COMM_H
#define NEURODRIVE_COMM_H

#include <Arduino.h>

void commSetup();

// Poll both links and dispatch any commands found. Returns the number of
// valid commands handled this iteration.
int commTick(unsigned long now);

bool commWifiConnected();
IPAddress commLocalIP();
const char* commModeName();

unsigned long commPacketsReceived();
unsigned long commPacketsRejected();
unsigned long commLastPacketMs();
IPAddress commLastPeer();

#endif  // NEURODRIVE_COMM_H
