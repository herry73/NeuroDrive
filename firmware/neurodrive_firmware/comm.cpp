#include "comm.h"

#include <WiFi.h>
#include <WiFiUdp.h>
#include <ctype.h>
#include <stdio.h>

#include "config.h"
#include "motor_control.h"
#include "safety.h"
#include "secrets.h"

static WiFiUDP udp;
static bool wifiUp = false;
static bool udpListening = false;

static unsigned long packetsReceived = 0;
static unsigned long packetsRejected = 0;
static unsigned long lastPacketMs = 0;
static IPAddress lastPeer;
static uint16_t lastPeerPort = 0;

static char serialLine[UDP_RX_BUFFER];
static size_t serialLineLength = 0;

// ---------------------------------------------------------------------------
// Command decoding
// ---------------------------------------------------------------------------

// Reduce a received line to its command character. Accepts a bare character
// ("F") or the long form ("FORWARD"); returns 0 if nothing is recognised.
static char decodeCommand(const char* text, size_t length) {
  // Skip leading whitespace.
  size_t start = 0;
  while (start < length && isspace((unsigned char)text[start])) {
    start++;
  }
  while (length > start && isspace((unsigned char)text[length - 1])) {
    length--;
  }
  if (start >= length) {
    return 0;
  }

  const char first = (char)toupper((unsigned char)text[start]);
  const size_t remaining = length - start;

  if (remaining == 1) {
    switch (first) {
      case CMD_CHAR_FORWARD:
      case CMD_CHAR_LEFT:
      case CMD_CHAR_RIGHT:
      case CMD_CHAR_STOP:
      case CMD_CHAR_PING:
        return first;
      default:
        return 0;
    }
  }

  // Long forms. Comparing only the first character plus the length keeps
  // this cheap and unambiguous for the four commands we define.
  if (remaining == 7 && first == 'F') return CMD_CHAR_FORWARD;   // FORWARD
  if (remaining == 4 && first == 'L') return CMD_CHAR_LEFT;      // LEFT
  if (remaining == 5 && first == 'R') return CMD_CHAR_RIGHT;     // RIGHT
  if (remaining == 4 && first == 'S') return CMD_CHAR_STOP;      // STOP
  if (remaining == 4 && first == 'P') return CMD_CHAR_PING;      // PING
  return 0;
}

static void sendAck(char command, bool overUdp) {
#if SEND_ACK
  char reply[40];
  const int written = snprintf(reply, sizeof(reply), "ACK:%c:%s\n", command,
                               motorStateName(motorGetState()));
  if (written <= 0) {
    return;
  }
  if (overUdp && udpListening) {
    const uint16_t port = (UDP_ACK_PORT != 0) ? (uint16_t)UDP_ACK_PORT : lastPeerPort;
    if (udp.beginPacket(lastPeer, port)) {
      udp.write((const uint8_t*)reply, (size_t)written);
      udp.endPacket();
    }
  } else {
    Serial.write((const uint8_t*)reply, (size_t)written);
  }
#else
  (void)command;
  (void)overUdp;
#endif
}

// Apply one decoded command. Returns true if it was a real command.
static bool applyCommand(char command, unsigned long now, bool overUdp) {
  switch (command) {
    case CMD_CHAR_FORWARD:
      motorSetState(STATE_FORWARD);
      break;
    case CMD_CHAR_LEFT:
      motorSetState(STATE_TURN_LEFT);
      break;
    case CMD_CHAR_RIGHT:
      motorSetState(STATE_TURN_RIGHT);
      break;
    case CMD_CHAR_STOP:
      motorSetState(STATE_STOP);
      break;
    case CMD_CHAR_PING:
      // Keepalive only: feeds the watchdog without changing the state.
      break;
    default:
      return false;
  }

  // SF-02: any valid command, including PING, keeps the watchdog happy.
  safetyFeedWatchdog(now);
  packetsReceived++;
  lastPacketMs = now;
  sendAck(command, overUdp);
  return true;
}

// ---------------------------------------------------------------------------
// WiFi
// ---------------------------------------------------------------------------

static void startAccessPoint() {
  WiFi.mode(WIFI_AP);
  const bool ok = WiFi.softAP(WIFI_SSID, WIFI_PASSWORD, WIFI_AP_CHANNEL);
  wifiUp = ok;
  if (ok) {
    Serial.print(F("[wifi] access point '"));
    Serial.print(WIFI_SSID);
    Serial.print(F("' up at "));
    Serial.print(WiFi.softAPIP());
    Serial.print(F(" on channel "));
    Serial.println(WIFI_AP_CHANNEL);
  } else {
    Serial.println(F("[wifi] FAILED to start access point"));
  }
}

static void startStation() {
  WiFi.mode(WIFI_STA);

#if WIFI_USE_STATIC_IP
  IPAddress ip(WIFI_STATIC_IP);
  IPAddress gateway(WIFI_STATIC_GATEWAY);
  IPAddress subnet(WIFI_STATIC_SUBNET);
  if (!WiFi.config(ip, gateway, subnet)) {
    Serial.println(F("[wifi] static IP configuration rejected, using DHCP"));
  }
#endif

  Serial.print(F("[wifi] joining '"));
  Serial.print(WIFI_SSID);
  Serial.print(F("' "));
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const unsigned long deadline = millis() + (WIFI_CONNECT_TIMEOUT_S * 1000UL);
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();

  wifiUp = (WiFi.status() == WL_CONNECTED);
  if (wifiUp) {
    Serial.print(F("[wifi] connected, IP "));
    Serial.println(WiFi.localIP());
    Serial.print(F("[wifi] put this address in python_bridge/config.json "
                   "as transport.udp.esp32_ip\n"));
  } else {
    Serial.println(F("[wifi] NOT connected -- falling back to serial control"));
    Serial.println(F("[wifi] set transport.mode=\"serial\" in config.json"));
  }
}

void commSetup() {
#if WIFI_USE_AP
  startAccessPoint();
#else
  startStation();
#endif

  if (wifiUp) {
    udpListening = udp.begin(UDP_COMMAND_PORT);
    if (udpListening) {
      Serial.print(F("[udp] listening on port "));
      Serial.println(UDP_COMMAND_PORT);
    } else {
      Serial.println(F("[udp] FAILED to bind command port"));
    }
  }

  Serial.println(F("[comm] serial control is always available: type F/L/R/S "
                   "then Enter"));
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

static int pollUdp(unsigned long now) {
  if (!udpListening) {
    return 0;
  }

  int handled = 0;
  int size = udp.parsePacket();
  while (size > 0) {
    char buffer[UDP_RX_BUFFER];
    const int length = udp.read(buffer, sizeof(buffer) - 1);
    if (length > 0) {
      buffer[length] = '\0';
      lastPeer = udp.remoteIP();
      lastPeerPort = udp.remotePort();

      // A datagram may legitimately carry several newline-separated
      // commands if the bridge bursts a turn.
      int start = 0;
      for (int i = 0; i <= length; i++) {
        if (i == length || buffer[i] == '\n' || buffer[i] == '\r') {
          if (i > start) {
            const char command = decodeCommand(buffer + start, (size_t)(i - start));
            if (command != 0 && applyCommand(command, now, true)) {
              handled++;
            } else if (command == 0) {
              packetsRejected++;
            }
          }
          start = i + 1;
        }
      }
    }
    size = udp.parsePacket();
  }
  return handled;
}

static int pollSerial(unsigned long now) {
  int handled = 0;
  while (Serial.available() > 0) {
    const int value = Serial.read();
    if (value < 0) {
      break;
    }
    const char character = (char)value;

    if (character == '\n' || character == '\r') {
      if (serialLineLength > 0) {
        const char command = decodeCommand(serialLine, serialLineLength);
        if (command != 0 && applyCommand(command, now, false)) {
          handled++;
        } else if (command == 0) {
          packetsRejected++;
          Serial.println(F("ERR:unknown command"));
        }
        serialLineLength = 0;
      }
      continue;
    }

    if (serialLineLength < sizeof(serialLine) - 1) {
      serialLine[serialLineLength++] = character;
    } else {
      // Overlong line: drop it rather than overflow the buffer (NFR 3.1).
      serialLineLength = 0;
      packetsRejected++;
    }
  }
  return handled;
}

int commTick(unsigned long now) {
  int handled = 0;
  handled += pollUdp(now);
  handled += pollSerial(now);
  return handled;
}

// ---------------------------------------------------------------------------
// Introspection
// ---------------------------------------------------------------------------

bool commWifiConnected() {
#if WIFI_USE_AP
  return wifiUp;
#else
  return wifiUp && WiFi.status() == WL_CONNECTED;
#endif
}

IPAddress commLocalIP() {
#if WIFI_USE_AP
  return WiFi.softAPIP();
#else
  return WiFi.localIP();
#endif
}

const char* commModeName() {
#if WIFI_USE_AP
  return "AP";
#else
  return "STA";
#endif
}

unsigned long commPacketsReceived() { return packetsReceived; }
unsigned long commPacketsRejected() { return packetsRejected; }
unsigned long commLastPacketMs() { return lastPacketMs; }
IPAddress commLastPeer() { return lastPeer; }
