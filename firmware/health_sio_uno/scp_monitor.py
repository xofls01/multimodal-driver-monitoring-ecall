"""Print decoded SCP Health frames from the Uno (health_sio_uno.ino).

Usage: py scp_monitor.py COM5        (Windows)
       python3 scp_monitor.py /dev/ttyACM0   (Raspberry Pi)
"""
import struct
import sys

HEADER = 0xA6
GID_HEALTH = 0x12


class FrameDecoder:
    """Byte-stream decoder. Resyncs on bad header/GID/checksum."""

    def __init__(self, max_payload=1024):
        self.buf = bytearray()
        self.max_payload = max_payload
        self.bad = 0

    def feed(self, data):
        self.buf.extend(data)
        frames = []
        while len(self.buf) >= 8:
            if self.buf[0] != HEADER or self.buf[4] != GID_HEALTH:
                del self.buf[0]
                self.bad += 1
                continue
            n = (self.buf[2] << 8) | self.buf[3]
            if n > self.max_payload:
                del self.buf[0]
                self.bad += 1
                continue
            if len(self.buf) < 8 + n:
                break
            frame = bytes(self.buf[:8 + n])
            expected = (sum(frame[:6 + n]) + 1) & 0xFFFF
            if struct.unpack('>H', frame[6 + n:])[0] != expected:
                del self.buf[0]
                self.bad += 1
                continue
            del self.buf[:8 + n]
            frames.append((frame[5], frame[6:6 + n]))
        return frames


def parse(cid, p):
    if cid == 0x09:
        seq, n, tick = struct.unpack_from('<HHI', p)
        samples = struct.unpack_from(f'<{n}H', p, 8)
        return dict(kind='ecg', seq=seq, tick=tick, mv=list(samples))
    if cid == 0x19:
        seq, n, tick = struct.unpack_from('<HHI', p)
        red, ir = [], []
        for i in range(n):
            o = 8 + 6 * i
            red.append(int.from_bytes(p[o:o + 3], 'little'))
            ir.append(int.from_bytes(p[o + 3:o + 6], 'little'))
        return dict(kind='ppg', seq=seq, tick=tick, red=red, ir=ir)
    if cid in (0x06, 0x16):
        running, rate, seq, dropped, extra = struct.unpack('<BHHHB', p)
        return dict(kind='status_ecg' if cid == 0x06 else 'status_ppg', running=running,
                    rate=rate, next_seq=seq, dropped=dropped, extra=extra)
    if cid in (0x07, 0x17):
        code, state, tick = struct.unpack('<BBI', p)
        name = {1: 'lead_off', 2: 'fifo_overflow'}.get(code, hex(code))
        return dict(kind='event', cid=hex(cid), event=name, state=state, tick=tick)
    if cid in (0x08, 0x18):
        return dict(kind='ack', cid=hex(cid), command=hex(p[0]), error=p[1])
    return dict(kind='unknown', cid=hex(cid), payload=p.hex())


def main():
    import serial
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    decoder = FrameDecoder()
    last_seq = {}
    with serial.Serial(sys.argv[1], 115200, timeout=1) as ser:
        while True:
            for cid, payload in decoder.feed(ser.read(256)):
                msg = parse(cid, payload)
                kind = msg['kind']
                if kind in ('ecg', 'ppg'):
                    prev = last_seq.get(kind)
                    gap = '' if prev is None or msg['seq'] == (prev + 1) & 0xFFFF else '  SEQ GAP'
                    last_seq[kind] = msg['seq']
                    values = msg['mv'] if kind == 'ecg' else msg['ir']
                    print(f"{kind} seq={msg['seq']} tick={msg['tick']} n={len(values)} "
                          f"first={values[0]} min={min(values)} max={max(values)}{gap}")
                else:
                    print(msg, f'(bad bytes so far: {decoder.bad})' if decoder.bad else '')


if __name__ == '__main__':
    main()
