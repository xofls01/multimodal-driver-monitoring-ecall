/*
 * LDSW Health SIO (SCP binary frame) firmware for Arduino Uno - ECG only.
 * Follows ECG_PPG_DATA_PROCESSING_REPORT.md section 3.1 and 4.
 *
 * Hardware: AD8232 ECG module. PPG is not sent (sensor out of service).
 *
 * Frame: A6 | Req | LenH | LenL | GID 0x12 | CID | Payload[N] | ChkH | ChkL
 *        Checksum = (sum(bytes[0..6+N-1]) + 1) & 0xFFFF, Big-Endian.
 * ECG block (CID 0x09): [seq:2 LE][count:2 LE][first_tick:4 LE][sample mV:2 LE]*count
 *
 * Status/Event/Ack payloads and the CPU->MCU header are not in the report
 * (see LDSW_FRONT_Health_SIO_Protocol_v0.1.md). The layouts below are
 * provisional; align them with that document when available.
 */
#include <Arduino.h>

// ---- Wiring ----
const uint8_t ECG_PIN = A0;        // AD8232 OUTPUT
const uint8_t LOD_PLUS_PIN = 10;   // AD8232 LO+
const uint8_t LOD_MINUS_PIN = 11;  // AD8232 LO-
const int8_t SDN_PIN = -1;         // AD8232 SDN, -1 if not connected

const bool AUTO_START = true;      // Stream on power-up without EcgStart
const uint16_t ADC_REF_MV = 5000;  // Uno default analog reference

// ---- Protocol ----
const uint8_t SCP_HEADER = 0xA6;
const uint8_t GID_HEALTH = 0x12;

const uint8_t CID_ECG_START = 0x01;
const uint8_t CID_ECG_STOP = 0x02;
const uint8_t CID_ECG_STATUS = 0x06;
const uint8_t CID_ECG_EVENT = 0x07;
const uint8_t CID_ECG_ACK = 0x08;
const uint8_t CID_ECG_BLOCK = 0x09;

// Provisional codes (not defined in the report)
const uint8_t ERR_OK = 0x00;
const uint8_t ERR_PARAM = 0x01;
const uint8_t EVT_LEAD_OFF = 0x01;
const uint8_t EVT_FIFO_OVERFLOW = 0x02;

// ---- Timing ----
// Timer1 base tick 1 kHz, one ADC read per tick. Each output sample is the
// mean of BASE_RATE_HZ / rate reads (4 at the report's 250 Hz). The boxcar
// acts as an anti-alias filter: mains harmonics at 180/240/480 Hz would
// otherwise fold into 70/10/20 Hz at 250 Hz sampling.
const uint16_t BASE_RATE_HZ = 1000;
const uint16_t MAX_RATE_HZ = 500;
const uint8_t ECG_BLOCK = 25;      // 100 ms at 250 Hz

// ---- Ring buffer (size must be a power of two) ----
const uint8_t ECG_RING = 64;

volatile uint16_t ecgVal[ECG_RING];
volatile uint32_t ecgTick[ECG_RING];
volatile uint8_t ecgHead = 0, ecgTail = 0;
volatile uint16_t ecgDropped = 0;
volatile bool ecgOverflow = false;

volatile bool ecgRunning = false;
volatile uint8_t ecgDivider = BASE_RATE_HZ / 250;
volatile uint8_t ecgPhase = 0;
volatile uint32_t ecgAccum = 0;

uint16_t ecgRateHz = 250;
uint16_t ecgSeq = 0;

bool leadOffReported = false;
bool leadOffCandidate = false;
uint32_t leadOffChangedMs = 0;
uint32_t lastStatusMs = 0;

ISR(TIMER1_COMPA_vect) {
  if (!ecgRunning) return;
  ecgAccum += analogRead(ECG_PIN);
  if (++ecgPhase < ecgDivider) return;
  uint32_t now = millis();
  uint16_t mv = ecgAccum * ADC_REF_MV / (1023UL * ecgDivider);
  ecgAccum = 0;
  ecgPhase = 0;
  uint8_t next = (ecgHead + 1) & (ECG_RING - 1);
  if (next == ecgTail) {
    ecgDropped++;
    ecgOverflow = true;
  } else {
    ecgVal[ecgHead] = mv;
    ecgTick[ecgHead] = now;
    ecgHead = next;
  }
}

// ---- Frame writer (checksum accumulated while streaming) ----
uint16_t txSum;

void txByte(uint8_t b) {
  Serial.write(b);
  txSum += b;
}

void txU16(uint16_t v) {
  txByte(v & 0xFF);
  txByte(v >> 8);
}

void txU32(uint32_t v) {
  txU16(v & 0xFFFF);
  txU16(v >> 16);
}

void beginFrame(uint8_t cid, uint16_t len) {
  txSum = 0;
  txByte(SCP_HEADER);
  txByte(0x00);  // Req: no response required
  txByte(len >> 8);
  txByte(len & 0xFF);
  txByte(GID_HEALTH);
  txByte(cid);
}

void endFrame() {
  uint16_t checksum = txSum + 1;
  Serial.write(checksum >> 8);
  Serial.write(checksum & 0xFF);
}

// ---- ECG block ----
void sendEcgBlock(uint8_t n) {
  uint8_t t = ecgTail;
  beginFrame(CID_ECG_BLOCK, 8 + 2 * n);
  txU16(ecgSeq++);
  txU16(n);
  txU32(ecgTick[t]);
  for (uint8_t i = 0; i < n; i++) {
    txU16(ecgVal[t]);
    t = (t + 1) & (ECG_RING - 1);
  }
  endFrame();
  ecgTail = t;  // Release slots only after they are sent
}

void pumpBlocks() {
  uint8_t avail = (ecgHead - ecgTail) & (ECG_RING - 1);
  if (avail >= ECG_BLOCK || (!ecgRunning && avail > 0)) {
    sendEcgBlock(min(avail, ECG_BLOCK));
  }
}

// ---- Event / status (provisional payloads) ----
// EventNotify: [event_code:1][state:1][tick:4 LE]
void sendEvent(uint8_t code, uint8_t state) {
  beginFrame(CID_ECG_EVENT, 6);
  txByte(code);
  txByte(state);
  txU32(millis());
  endFrame();
}

void checkLeadOff() {
  bool off = digitalRead(LOD_PLUS_PIN) == HIGH || digitalRead(LOD_MINUS_PIN) == HIGH;
  uint32_t now = millis();
  if (off != leadOffCandidate) {
    leadOffCandidate = off;
    leadOffChangedMs = now;
  }
  // Report after 50 ms of stable state to ignore contact chatter
  if (leadOffCandidate != leadOffReported && now - leadOffChangedMs >= 50) {
    leadOffReported = leadOffCandidate;
    sendEvent(EVT_LEAD_OFF, leadOffReported ? 1 : 0);
  }
}

void checkOverflow() {
  noInterrupts();
  bool overflow = ecgOverflow;
  ecgOverflow = false;
  interrupts();
  if (overflow) sendEvent(EVT_FIFO_OVERFLOW, 1);
}

// StatusReport: [running:1][sample_rate:2 LE][next_seq:2 LE][dropped:2 LE][lead_off:1]
void sendStatus() {
  noInterrupts();
  uint16_t dropped = ecgDropped;
  interrupts();
  beginFrame(CID_ECG_STATUS, 8);
  txByte(ecgRunning ? 1 : 0);
  txU16(ecgRateHz);
  txU16(ecgSeq);
  txU16(dropped);
  txByte(leadOffReported ? 1 : 0);
  endFrame();
}

// ---- Commands (CPU -> MCU) ----
// EcgConfigAck: [command_cid:1][error_code:1]
void sendAck(uint8_t cmd, uint8_t err) {
  beginFrame(CID_ECG_ACK, 2);
  txByte(cmd);
  txByte(err);
  endFrame();
}

void setEcgRunning(bool run) {
  noInterrupts();
  ecgRunning = run;
  ecgPhase = 0;
  ecgAccum = 0;
  if (run) ecgTail = ecgHead;  // Discard stale samples on start
  interrupts();
  if (SDN_PIN >= 0) digitalWrite(SDN_PIN, run ? HIGH : LOW);
}

void handleCommand(uint8_t cid, const uint8_t *p, uint16_t n) {
  switch (cid) {
    case CID_ECG_START: {
      // Optional payload: [sample_rate:2 LE]. Must divide the 1 kHz base tick.
      uint16_t rate = n >= 2 ? (uint16_t)(p[0] | (p[1] << 8)) : ecgRateHz;
      if (rate < BASE_RATE_HZ / 255 || rate > MAX_RATE_HZ || BASE_RATE_HZ % rate != 0) {
        sendAck(cid, ERR_PARAM);
        return;
      }
      ecgRateHz = rate;
      noInterrupts();
      ecgDivider = BASE_RATE_HZ / rate;
      interrupts();
      setEcgRunning(true);
      sendAck(cid, ERR_OK);
      break;
    }
    case CID_ECG_STOP:
      setEcgRunning(false);
      sendAck(cid, ERR_OK);
      break;
  }
}

// Same frame layout as MCU->CPU. The CPU->MCU header byte is not given in
// the report, so any header is accepted; GID and checksum guard framing.
const uint8_t RX_MAX = 24;
uint8_t rx[RX_MAX];
uint8_t rxLen = 0;

void rxShift(uint8_t count) {
  memmove(rx, rx + count, rxLen - count);
  rxLen -= count;
}

void pollCommands() {
  while (Serial.available()) {
    rx[rxLen++] = Serial.read();
    while (rxLen >= 8) {
      uint16_t n = ((uint16_t)rx[2] << 8) | rx[3];
      if (rx[4] != GID_HEALTH || n > RX_MAX - 8) {
        rxShift(1);
        continue;
      }
      if (rxLen < 8 + n) break;
      uint16_t sum = 1;
      for (uint8_t i = 0; i < 6 + n; i++) sum += rx[i];
      uint16_t got = ((uint16_t)rx[6 + n] << 8) | rx[7 + n];
      if (sum != got) {
        rxShift(1);
        continue;
      }
      handleCommand(rx[5], rx + 6, n);
      rxShift(8 + n);
    }
  }
}

void setup() {
  pinMode(LOD_PLUS_PIN, INPUT);
  pinMode(LOD_MINUS_PIN, INPUT);
  if (SDN_PIN >= 0) {
    pinMode(SDN_PIN, OUTPUT);
    digitalWrite(SDN_PIN, LOW);
  }
  Serial.begin(115200);  // 8N1

  // Timer1 CTC: 16 MHz / 64 / 1 kHz
  noInterrupts();
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1 = 0;
  OCR1A = F_CPU / 64 / BASE_RATE_HZ - 1;
  TCCR1B = _BV(WGM12) | _BV(CS11) | _BV(CS10);
  TIMSK1 = _BV(OCIE1A);
  interrupts();

  if (AUTO_START) setEcgRunning(true);
  lastStatusMs = millis();
}

void loop() {
  pollCommands();
  pumpBlocks();
  checkLeadOff();
  checkOverflow();
  if (millis() - lastStatusMs >= 1000) {
    lastStatusMs += 1000;
    sendStatus();
  }
}
