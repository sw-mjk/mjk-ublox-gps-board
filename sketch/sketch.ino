/*
 * MJK Ublox GPS Board - MCU firmware
 * =====================================
 * Responsibilities:
 *   1) Receive correction data from Linux (as a hex string), decode it and write it to the X20P
 *   2) Read the X20P's NMEA output and forward each sentence back to Linux
 *
 * Wiring: connect the X20P UART TX/RX to D0/D1 and share a common ground.
 *
 * WARNING - read before changing anything:
 *   - Serial is the App Lab console; D0/D1 is Serial1. Do not mix them up.
 *   - The bridge only reliably supports int/float/bool/String, so binary data must be
 *     passed as a hex string, and a single message has a length limit, so data is sent
 *     in small chunks (see SPARTN_CHUNK on the Linux side).
 */

#include <Arduino.h>
#include <Arduino_RouterBridge.h>

#define GNSS_SERIAL Serial1     // D0/D1 = USART1
#define GNSS_BAUD   38400       // X20P default

static uint8_t hexVal(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return 0;
}

// Linux sends a hex string -> decode it into bytes written to the X20P
static void pushBytes(String hex) {
  for (size_t i = 0; i + 1 < hex.length(); i += 2) {
    uint8_t b = (hexVal(hex[i]) << 4) | hexVal(hex[i + 1]);
    GNSS_SERIAL.write(b);
  }
}

void setup() {
  Bridge.begin();
  GNSS_SERIAL.begin(GNSS_BAUD);
  Bridge.provide("push_bytes", pushBytes);
}

// Accumulate incoming bytes into a full line (terminated by \n), then notify Linux
static char line[256];
static size_t lineLen = 0;

void loop() {
  while (GNSS_SERIAL.available()) {
    uint8_t b = GNSS_SERIAL.read();
    if (b == '\n') {
      if (lineLen > 0 && line[0] == '$') {
        line[lineLen] = '\0';
        Bridge.notify("gnss", String(line));   // MCU -> Linux (String)
      }
      lineLen = 0;
    } else if (b == '\r') {
      // ignore carriage return
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = (char)b;
    }
  }
  delay(1);
}
