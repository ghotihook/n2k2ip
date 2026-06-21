# TODO

## Investigate / reduce gateway CPU on the CoreMP135 (single-core Cortex-A7)

Observed `n2k2ip.py` using ~23% of the single A7 core at ~300 msg/s with 3
consumers — higher than the visible per-frame work predicts. Not urgent (no
drops, gateway keeps up), but worth revisiting since that core is oversubscribed
(load avg ~1.5).

### Profile first — don't optimize on theory

The cost model doesn't explain 23%: the necessary per-frame work (1 recv, 3 send,
struct.unpack, timestamp, deque ops) should be a few percent, ~5% even counting
the avoidable syscalls below. 23% implies ~770 us/frame, far more than this code
should take. Could be a startup/burst artifact in the sample window, or a hot
spot we haven't named. Confirm before rewriting:

```sh
sudo py-spy top --pid <n2k2ip pid>      # 30 s is enough
```

### Findings so far

- **Timestamp conversion is correct and cheap.** `frame_to_ydraw` verified
  correct across sub-second offsets and second/minute rollovers. Costs only
  ~0.5% of the core at 300/s — NOT the hot path. Caching the `HH:MM:SS`
  second-string is a ~3x win but saves ~0.3%; only worth doing while already in
  the function.

### Candidate improvements (in order of value, pending profile)

1. **Coalesce per-client writes.** `_flush` currently does one `send()` per
   buffered line. Join pending lines and send once per event.
2. **Skip redundant `sel.modify()`.** `_want_write` calls `sel.modify` (an
   `epoll_ctl`) for every client on every fan-out, even when the mask is
   unchanged (~900 needless syscalls/s in steady state). Track the registered
   mask per client and only modify on change.
3. **Replace per-client `deque` of small `bytes` with one `bytearray` + sent
   offset.** Fewer objects, one contiguous buffer; staleness becomes a byte cap
   trimmed to a `\r\n` boundary.
4. **Cache the `HH:MM:SS` second-string** (free 3x on the timestamp) — only while
   already editing `frame_to_ydraw`.

### Watch out

- Batching (#1/#3) trades against the line-granular freshness/drop-oldest
  guarantee and the per-frame `TCP_NODELAY` low-latency goal. At 300/s batches
  are ~1 line so it's free in practice — but never add a deliberate batching
  delay, that fights the "live data" design.
- Trimming the buffer must never drop a *partially-sent* head line, or one YDRAW
  line gets corrupted on the wire (same edge as the current deque drop-oldest).

Lowest-risk first pass once profiled: #1 + #2, measure before/after on the box.
