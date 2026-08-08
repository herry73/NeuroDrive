/*
 * Communication: WiFi/UDP command receiver plus USB serial fallback.
 *
 * Requirement coverage:
 *   COM-02  Receives commands over UDP (primary) or USB serial (fallback).
 *   COM-04  Non-blocking receive; a dropped packet simply means the previous
 *           command stays in force until the next one or the watchdog.
 *   COM-05  Acknowledges every accepted command.
 *   NFR 3.2 UDP, so there is no connection to re-establish and no
 *           retransmission stalling the control loop.
 *   NFR 3.3 Nothing here blocks: every read is polled, never waited on.
 *
 * Wire format (docs/INTERFACE_CONTRACT.md): one ASCII character terminated
 * by newline. The long forms ("FORWARD", "STOP", ...) are also accepted so
 * the protocol has room to grow (NFR 3.6) and so a human can drive the
 * vehicle from a plain serial monitor.
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
