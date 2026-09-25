"""Bounded, one-frame reader for Bambu A1/P1 JPEG-over-TLS cameras."""
import socket
import ssl
import struct
import time

MAX_FRAME = 8 * 1024 * 1024


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('Camera capture deadline exceeded')
    return seconds


def read_exact(stream, size, deadline):
    data = bytearray()
    while len(data) < size:
        stream.settimeout(remaining(deadline))
        chunk = stream.recv(min(65536, size - len(data)))
        if not chunk:
            raise ConnectionError('Camera closed the connection before a complete frame')
        data.extend(chunk)
    return bytes(data)


def capture_once(printer, deadline):
    code = str(printer['access_code']).encode('ascii')
    if len(code) > 32:
        raise ValueError('Invalid camera access-code length')
    auth = struct.pack('<IIII32s32s', 0x40, 0x3000, 0, 0, b'bblp', code)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Printer LAN certificates are self-signed, matching the existing LAN client.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((printer['ip'], 6000), timeout=min(5, remaining(deadline))) as raw:
        raw.settimeout(min(5, remaining(deadline)))
        with context.wrap_socket(raw, server_hostname=printer['ip']) as stream:
            stream.settimeout(remaining(deadline))
            stream.sendall(auth)
            # TCP may split headers or combine header/frame bytes: read exact lengths.
            while True:
                header = read_exact(stream, 16, deadline)
                size = int.from_bytes(header[:4], 'little')
                if not 4 <= size <= MAX_FRAME:
                    raise ValueError('Invalid camera frame length')
                frame = read_exact(stream, size, deadline)
                if frame.startswith(b'\xff\xd8\xff') and frame.endswith(b'\xff\xd9'):
                    return frame
                # A malformed frame need not poison the next complete frame.


def capture_jpeg(printer, timeout=22):
    deadline = time.monotonic() + timeout
    last_error = None
    for attempt in range(2):
        try:
            # Reserve time for one reconnect if the first connection stalls.
            attempt_deadline = min(deadline, time.monotonic() + (timeout / 2 if attempt == 0 else timeout))
            return capture_once(printer, attempt_deadline)
        except (OSError, ValueError) as error:
            last_error = error
            if attempt == 0 and deadline - time.monotonic() > 0.5:
                time.sleep(0.25)
            else:
                break
    raise last_error or TimeoutError('Camera capture timed out')
