#!/usr/bin/env python3
"""n2k2ip — bridge an NMEA2000 (SocketCAN) bus to TCP clients as Yacht Devices RAW.

Reads raw CAN frames from a SocketCAN channel and fans each one out, as a single
YDRAW line, to every connected TCP client.

Designed for *live* data, where stale data is worthless:
  - Each client has a bounded outbound queue. If a client can't keep up (a slow or
    stalled wifi link), the OLDEST whole lines are dropped so it always receives
    recent frames, never a growing backlog. Freshness is bounded by MAX_QUEUE.
  - Writes are non-blocking, so one slow/dead client never stalls the CAN reader or
    the other clients.
  - TCP_NODELAY on every client: each line hits the wire immediately (no Nagle).

Single-threaded, event-driven (selectors) — no threads, no busy-poll.

    python3 n2k2ip.py --channel can0 --port 1457
"""
import argparse
import logging
import selectors
import socket
import struct
import time
from collections import deque

__version__ = "0.1.0"

# struct can_frame (Linux): u32 can_id, u8 dlc, 3 pad bytes, 8 data bytes
CAN_FRAME    = struct.Struct("=IB3x8s")
CAN_RTR_FLAG = 0x40000000   # remote-transmission request
CAN_ERR_FLAG = 0x20000000   # error frame
CAN_EFF_MASK = 0x1FFFFFFF   # 29-bit extended identifier

# Max whole lines buffered per client before the oldest are dropped. Bounds how
# stale a slow client's data can get; favours freshness over completeness.
MAX_QUEUE = 100

log = logging.getLogger("n2k2ip")


def frame_to_ydraw(can_id: int, data: bytes) -> bytes:
    """Render a CAN frame as one YDRAW line: 'HH:MM:SS.mmm R <8-hex id> <hex bytes>'."""
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.gmtime(now)) + ".%03d" % int((now % 1) * 1000)
    body = data.hex(" ").upper()
    line = "%s R %08X" % (stamp, can_id)
    return ((line + " " + body) if body else line).encode("ascii") + b"\r\n"


class Client:
    __slots__ = ("sock", "addr", "buf", "dropped")

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = "%s:%d" % addr
        self.buf = deque()   # pending whole YDRAW lines (bytes); each is atomic
        self.dropped = 0


class CanDown(Exception):
    """The CAN socket errored (interface down / bus-off) and must be reopened."""


class Gateway:
    def __init__(self, channel: str, port: int):
        self.channel = channel
        self.max_queue = MAX_QUEUE
        self.clients: set[Client] = set()
        self.sel = selectors.DefaultSelector()
        self.frames = 0

        self.listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listen.bind(("", port))
        self.listen.listen(16)
        self.listen.setblocking(False)
        self.sel.register(self.listen, selectors.EVENT_READ, "listen")

        self.can = self._open_can()
        self.sel.register(self.can, selectors.EVENT_READ, "can")
        log.info("listening on tcp/%d, reading %s", port, channel)

    # ── CAN ────────────────────────────────────────────────────────────────────
    def _open_can(self):
        while True:
            try:
                s = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
                s.bind((self.channel,))
                s.setblocking(False)
                return s
            except OSError as e:
                log.warning("%s not available (%s) — retrying; bring it up: "
                            "sudo ip link set %s up type can bitrate 250000",
                            self.channel, e, self.channel)
                time.sleep(1.0)

    def _drain_can(self) -> list:
        """Read every frame currently available; return their YDRAW lines."""
        lines = []
        while True:
            try:
                raw = self.can.recv(16)
            except BlockingIOError:
                break
            except OSError as e:
                raise CanDown(str(e))
            if len(raw) < 16:
                continue
            can_id, dlc, payload = CAN_FRAME.unpack(raw)
            if can_id & (CAN_ERR_FLAG | CAN_RTR_FLAG):
                continue   # skip error/remote frames
            lines.append(frame_to_ydraw(can_id & CAN_EFF_MASK, payload[:dlc]))
        self.frames += len(lines)
        return lines

    # ── clients ────────────────────────────────────────────────────────────────
    def _accept(self):
        try:
            sock, addr = self.listen.accept()
        except OSError:
            return
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        c = Client(sock, addr)
        self.clients.add(c)
        self.sel.register(sock, selectors.EVENT_READ, c)
        log.info("client %s connected (%d total)", c.addr, len(self.clients))

    def _drop(self, c: Client, reason: str):
        if c not in self.clients:
            return   # idempotent: already dropped this batch
        self.clients.discard(c)
        try:
            self.sel.unregister(c.sock)
        except (KeyError, ValueError):
            pass     # never registered / fd already closed
        try:
            c.sock.close()
        except OSError:
            pass
        log.info("client %s gone: %s (dropped %d lines; %d total)",
                 c.addr, reason, c.dropped, len(self.clients))

    def _flush(self, c: Client) -> bool:
        """Send as much of the client's queue as the socket accepts without blocking.
        Returns True if fully drained. Raises OSError if the client is dead."""
        while c.buf:
            chunk = c.buf[0]
            try:
                n = c.sock.send(chunk)
            except BlockingIOError:
                return False
            if n < len(chunk):
                c.buf[0] = chunk[n:]
                return False
            c.buf.popleft()
        return True

    def _want_write(self, c: Client):
        ev = selectors.EVENT_READ | (selectors.EVENT_WRITE if c.buf else 0)
        try:
            self.sel.modify(c.sock, ev, c)
        except (KeyError, ValueError):
            pass

    def _fan_out(self, lines: list):
        for c in list(self.clients):
            c.buf.extend(lines)
            # Bound staleness: keep only the most recent max_queue lines.
            excess = len(c.buf) - self.max_queue
            for _ in range(excess if excess > 0 else 0):
                c.buf.popleft()
                c.dropped += 1
            try:
                self._flush(c)
            except OSError as e:
                self._drop(c, str(e))
                continue
            self._want_write(c)

    def _service(self, c: Client, mask):
        if mask & selectors.EVENT_READ:
            # We don't expect input; a readable client means data or a closed peer.
            try:
                if not c.sock.recv(4096):
                    self._drop(c, "closed by peer")
                    return
            except OSError as e:
                self._drop(c, str(e))
                return
        if mask & selectors.EVENT_WRITE:
            try:
                self._flush(c)
            except OSError as e:
                self._drop(c, str(e))
                return
            self._want_write(c)

    # ── main loop ────────────────────────────────────────────────────────────────
    def run(self):
        last_stat = time.monotonic()
        while True:
            for key, mask in self.sel.select(timeout=5.0):
                tag = key.data
                if tag == "can":
                    try:
                        self._fan_out(self._drain_can())
                    except CanDown as e:
                        log.error("CAN error (%s) — reopening %s", e, self.channel)
                        self.sel.unregister(self.can)
                        self.can.close()
                        self.can = self._open_can()
                        self.sel.register(self.can, selectors.EVENT_READ, "can")
                elif tag == "listen":
                    self._accept()
                elif tag in self.clients:
                    self._service(tag, mask)
                # else: a stale event for a client already dropped earlier in this
                # same select() batch — ignore it.

            now = time.monotonic()
            if now - last_stat >= 30:
                log.info("stats: %d clients, %d frames forwarded", len(self.clients),
                         self.frames)
                last_stat = now

    def close(self):
        for c in list(self.clients):
            c.sock.close()
        self.listen.close()
        self.can.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="can0", help="SocketCAN interface (default: can0)")
    ap.add_argument("--port", type=int, default=1457,
                    help="TCP port to serve YDRAW on (default: 1457)")
    ap.add_argument("--log-level", default="INFO",
                    choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    ap.add_argument("--version", action="version",
                    version=f"n2k2ip {__version__}")
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s [n2k2ip] %(levelname)s %(message)s")
    gw = Gateway(args.channel, args.port)
    try:
        gw.run()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        gw.close()


if __name__ == "__main__":
    main()
