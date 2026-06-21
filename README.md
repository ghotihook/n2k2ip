# n2k2ip

Bridge an NMEA 2000 (SocketCAN) bus onto the network: read raw CAN frames from a
SocketCAN interface and stream them to TCP clients as **Yacht Devices RAW (YDRAW)**
text — one line per CAN frame. A software equivalent of a Yacht Devices Wi-Fi gateway
(YDWG-02) in *RAW* mode.

```
NMEA2000 bus ──(can0)──▶ n2k2ip ──(tcp/1457)──▶ many clients
                                                 (plotters, loggers, OpenCPN, …)
```

## Why it exists / design

The data is **live**, and stale data is worthless. Everything here serves that:

- **Drop-old, never back up.** Each client has a bounded outbound queue. When a client
  can't keep up (a slow or stalled Wi-Fi link), the *oldest* whole lines are dropped so
  it always receives recent frames instead of a growing backlog. Maximum staleness is
  bounded by `--max-queue`.
- **One slow client can't hurt the others.** All writes are non-blocking; a stalled or
  dead client never blocks the CAN reader or the other clients.
- **Low latency by default.** `TCP_NODELAY` is set on every client, so each line hits
  the wire immediately (no Nagle batching). Over Wi-Fi this matters — measured median
  latency for this kind of small-message stream is a few ms with NODELAY vs tens of ms
  without.
- **Lightweight.** Single-threaded, event-driven (`selectors`) — no threads, no
  busy-poll, no dependencies. Pure Python standard library.

## Requirements

- Linux with SocketCAN (a `can0`-style interface)
- Python **3.9+** — **no third-party packages** (stdlib only)
- A **synchronised host clock** — YDRAW timestamps are taken from the host's UTC
  clock, so make sure NTP is running and disciplined (e.g. `systemd-timesyncd` or
  `chrony`). On a headless/boat box without a network, fit an RTC or expect the
  clock — and therefore the timestamps — to be wrong until time is acquired.

## Quick start

```sh
# bring the CAN interface up at the NMEA2000 bitrate
sudo ip link set can0 up type can bitrate 250000

# serve YDRAW on tcp/1457
python3 n2k2ip.py --channel can0 --port 1457
```

Connect and watch:

```sh
nc <host> 1457
# 09:46:57.556 R 0DF50B16 00 E6 05 00 00 FF 7F FF
# 09:46:57.556 R 00FA8C3E 4C 08 C2 24 7E E8 61 4B
# ...
```

## Usage

```
python3 n2k2ip.py [--channel can0] [--port 1457] [--max-queue 1000] [--log-level INFO]
```

| option | default | meaning |
|---|---|---|
| `--channel` | `can0` | SocketCAN interface to read |
| `--port` | `1457` | TCP port to serve YDRAW on |
| `--max-queue` | `1000` | Max lines buffered per client before the oldest are dropped. Lower = fresher data, more drops under congestion; higher = more tolerance for brief stalls, more potential staleness. ~1000 lines is roughly 2–3 s of backlog on a busy bus. |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Output format (YDRAW)

Each line is one CAN frame:

```
HH:MM:SS.mmm R <8-hex CAN-ID> <space-separated hex data bytes>
```

Lines are terminated with `\r\n`. Direction is always `R` (received). The
timestamp is the host clock in **UTC** (`HH:MM:SS.mmm`). The YDRAW spec calls for
UTC sourced from the NMEA 2000 bus when available; we don't decode the bus, so we
use the host's UTC clock instead — accurate as long as the host clock is synced
(e.g. NTP).

**Multi-frame (fast-packet) messages are passed through, not reassembled** — and that
is correct for YDRAW, which is a per-CAN-frame format. Each fast-packet frame is sent
as its own line with its sequence/frame-counter byte intact; reassembling them into a
logical PGN is the *consumer's* job (e.g. canboat `analyzer`, OpenCPN, a YDRAW
decoder). Over a lossy link this means a lost frame loses its whole fast-packet PGN —
inherent to the format, same as a hardware gateway. For that reason **TCP is preferred
over UDP/broadcast on Wi-Fi**, where reliability and low jitter matter most.

## Run as a service (systemd)

```sh
sudo mkdir -p /opt/n2k2ip
sudo cp n2k2ip.py /opt/n2k2ip/
sudo cp n2k2ip.service /etc/systemd/system/
# edit the unit if your channel/port differ, or if can0 is brought up elsewhere
sudo systemctl daemon-reload
sudo systemctl enable --now n2k2ip.service
journalctl -u n2k2ip.service -f
```

Because the script is stdlib-only, "install" is just copying the one file — no venv,
no `pip`.

## Behaviour notes

- **CAN reconnect.** If the interface goes down or the controller goes bus-off, the
  reader logs it and reopens the socket with backoff — appropriate for a boat bus that
  may power-cycle.
- **Error/remote frames** are skipped; only data frames are forwarded.
- **No filtering.** Every data frame on the bus is forwarded. Add a SocketCAN filter in
  `_open_can` if you need to restrict PGNs.

## License

MIT
