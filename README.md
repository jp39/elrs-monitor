# ELRS Monitor

A standalone, **receive-only** HackRF ExpressLRS decoder with a live channel
TUI, text/JSON output, frame logging, and deterministic IQ replay.

**Target:** ExpressLRS 4.1.0 / OTA-v4, **2.4 GHz LoRa**, non-Gemini. Six
LoRa profiles and all corresponding switch modes are configurable. **250Hz /
Hybrid and 50Hz / Wide are validated on physical RF recordings. Other PHY
profiles remain experimental and synthetic-tested.**
The receiver captures a fixed slice of the hopping band, not a complete control
stream. FLRC/GFSK modes such as D250 and F1000 are deliberately excluded.

## Install

Requires Linux/Unix, Python **3.11+**, NumPy, SciPy, and (for RF reception)
`hackrf_transfer`. The TUI uses Python's standard `curses` module; there is no
GUI/TUI framework dependency. Some distributions package curses separately.

```bash
cd ~/elrs-monitor
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

On this computer the environment is already installed, using the system's
NumPy/SciPy via a `--system-site-packages` virtual environment. No reference
source checkouts or files in the old analysis directory are runtime dependencies.

Install the HackRF utilities if needed, e.g. on Fedora:

```bash
pkexec dnf install hackrf
```

## Command-line settings

All settings come from **command-line flags and their documented defaults**.
There is no configuration-file loading, discovery, or persistence. The old
`--config` and `--no-config` flags have been removed. An existing
`config.local.json` is left untouched but is never read.

**Choose exactly one identity on every invocation** (except help/version):

- `--bind-phrase PHRASE`: derive the UID from a known binding phrase.
- `--uid HEX`: use a known four- or six-byte hexadecimal UID directly.
- `--discover-binding`: passively learn the effective UID during transmitter
  **Bind mode**, then continue normal monitoring automatically. No phrase,
  receiver, or transmitting required.

Other flags are optional:

| Setting | Default |
| --- | --- |
| Packet rate | 250Hz; binding discovery automatically uses 50Hz |
| Switch mode | Hybrid |
| Model Match | Off |
| Model ID | 0; ignored while Model Match is off |
| Capture source | Live HackRF; first available device |
| Text output | Human-readable; use `--tui` or `--format json` to change |
| DSP workers | 2 |
| RX LNA / VGA gain | 16 / 16 dB; RF amplifier and antenna bias off |
| Stale threshold | 1 second |
| Acquisition duration | Until stopped/EOF; `--seconds` limits the entire session |
| Replay speed | As fast as possible; use `--pace` for real time |
| JSONL logging / radio diagnostics | Off |
| Processed hop channels | All nine; binding acquisition always includes the sync channel |

Minimal command for a 250Hz/Hybrid transmitter with Model Match off:

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui
```

For your current transmitter with Model Match **on, ID 11**, supply those flags
explicitly; the old file no longer provides them:

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui \
  --rate 250 --switch-mode Hybrid --model-match on --model-id 11

# Another LoRa rate/mode, with Model Match off (the default).
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui \
  --rate 150 --switch-mode Wide

# Full-resolution LoRa example (experimental PHY).
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui \
  --rate 333 --switch-mode 16ch
```

`--packet-rate`, `--refresh-rate`, and `--rate` are aliases. Numeric values,
`250Hz`, and quoted names such as `'333Hz Full'` are accepted. `--binding-phrase`
is an alias for `--bind-phrase`. Model Match accepts `--model-match on`,
`--model-match off`, bare `--model-match` (on), or `--no-model-match` (off).
IDs are 0–63. **Setting an ID does not implicitly enable Model Match.** When
off, identity validation uses the unmodified binding UID; it does not accept
unknown phrases. ELRS's mask for ID 63 is zero, so that on-air identity is
indistinguishable from Model Match off.

These flags configure the listener, not the transmitter. Their settings must
match the transmitter. See `.venv/bin/elrs-monitor --help` for all options,
defaults, combinations, identity details, and examples.

### Displayed identity

The binding phrase is a **deterministic identity seed**, not a secret,
encryption key, or authentication mechanism. It is not masked or hidden.
At startup the program prints the supplied phrase and its full base UID, plus
the four-byte link suffix and Model Match-adjusted sync bytes. In the TUI the
base UID and sync bytes stay visible in a dedicated header row.

For example, `--bind-phrase julian --model-match on --model-id 0` shows:

```text
Binding phrase 'julian' -> base UID 16:b3:13:ad:5f:d3
Link UID 13:ad:5f:d3 | sync UID bytes 5f:ec
```

ELRS derives the base UID as the first six bytes of
`MD5('-DMY_BINDING_PHRASE="julian"')`. **Model Match does not change that base
UID**: it XORs only the final sync identity byte with `(~model_id) & 0x3f`.
Use the **base UID** shown above with `--uid`, not the adjusted sync bytes:

```bash
.venv/bin/elrs-monitor --uid 16:b3:13:ad:5f:d3 --rate 50 \
  --switch-mode Wide --model-match on --model-id 0
```

Identity messages go to stderr in text/JSON mode, so JSON stdout remains a
stream of frame records. The final status summary also includes identity
details, including the supplied phrase. Frame JSONL logs remain packet records,
not a separate settings dump. Discovery has no identity until one is learned.

### Direct UID and passive binding discovery

`--uid` accepts four or six **hexadecimal bytes**, with colons, hyphens, or no
separators. For example, `12:34:56:78`, `12345678`, and
`00:00:12:34:56:78` select the same effective link identity. Four-byte inputs
are padded with `00:00`. All-zero final four bytes are an unbound identity
and are rejected. Decimal comma-separated lists are not accepted.

```bash
.venv/bin/elrs-monitor --uid 00:00:12:34:56:78 --rate 50 --tui
# Add the matching --switch-mode / --model-match / --model-id as necessary.
```

For discovery, start the listener **before** putting one transmitter into
Bind mode through its ELRS controls:

```bash
.venv/bin/elrs-monitor --discover-binding --rate 50 --switch-mode Wide \
  --model-match on --model-id 0 --tui
# Optional: limit the whole session and log discovery plus normal frames.
.venv/bin/elrs-monitor --discover-binding --rate 50 --switch-mode Wide \
  --model-match on --model-id 0 --seconds 60 --jsonl session.jsonl
```

The listener uses the OTA-v4 binding CRC seed and nonce, 50Hz LoRa, and the
2440.4MHz sync channel. It requires **three separate messages with the same
UID within two seconds**. Duplicated DSP observations do not count; IQ gaps
reset confirmation state. Other packet types, wrong CRCs, nonbinding commands,
and other channels cannot confirm a UID. Only one binding transmitter should
be active: the first UID to reach the threshold wins. This is error detection
and repetition checking, not cryptographic authentication.

Binding messages disclose only **UID[2:6]**, the four bytes used for the hop
seed and CRC. The displayed `00:00` prefix is reconstructed, exactly as a
receiver does; it is **not** the original six-byte MAC or phrase-derived UID,
and discovery does not recover a phrase. Model Match is not learned from
binding packets; set it explicitly on a later monitoring run.

**Capture continues after confirmation.** A fresh known-link decoder adopts
the learned UID and waits for a matching normal sync as the transmitter exits
Bind mode. The dashboard switches from binding acquisition to the normal
channel view: UID learned does **not** mean channels are live yet. No raw
capture restart, sample-clock reset, or log rotation occurs. The UID remains
known across subsequent IQ gaps; sync/grid state resets as usual.

EOF/the sample-time limit before learning a UID returns an error. EOF after
learning a UID is a normal end, even if no matching normal sync arrived; the
status then remains `awaiting_sync`, with no invented channels. Press `q` or
Ctrl-C to quit a live session. There is no discovery-only mode or implicit
30-second limit; `--seconds` includes both acquisition and monitoring.
No settings are saved. JSON/JSONL records contain `binding_candidate` and
`binding_discovered`, followed by validated normal sync/RC/data frames. Neither
binding record alone is an RC update or sync lock.

`--rate`, `--switch-mode`, `--model-match`, and `--model-id` specify the
transmitter's **normal** settings, not binding settings. They must match its
configuration after Bind mode ends. Defaults remain 250Hz/Hybrid, Model Match
off, ID 0; normal rate/mode discovery is not implemented. Binding acquisition
always uses 50Hz on 2440.4MHz independently of those settings. Incompatible
normal rate/mode combinations are rejected before capture starts.

Do not supply a phrase or UID alongside `--discover-binding`. The default
channel selection is all nine; a custom `--offset` selection must include
`400000`. Replay uses the same signed-int8 10Msps/2440MHz format and supports
handover even when binding and normal packets use different PHY profiles.

**Selecting ordinary 50Hz operation is not entering Bind mode.** Normal
traffic does not announce a binding UID; monitor it with a phrase or known UID.
For a normal 50Hz/Hybrid test with Model Match ID 11:

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --rate 50 \
  --switch-mode Hybrid --model-match on --model-id 11 --tui
```

Allow several seconds for a sync-channel visit, then move sticks and switches.
Both normal 50Hz/Wide reception and **binding discovery are validated against
physical RF captures**. The binding capture yielded three identical CRC-valid
messages over 80ms, recovering `00:00:13:ad:5f:d3`, exactly the link suffix of
the base UID derived from `julian`. To monitor using that discovered identity:

```bash
.venv/bin/elrs-monitor --uid 00:00:13:ad:5f:d3 --rate 50 \
  --switch-mode Wide --model-match on --model-id 0 --tui
```

Version 0.4.3 fixes detection of the >50% channel occupancy during Bind mode.
Ordinary hopping traffic did not expose this issue. See `docs/validation.md`
for the scope of the recorded-RF and lifecycle checks.

### LoRa profile and switch-mode combinations

| Rate | SF | FEC | Packet | Switch modes |
| --- | --- | --- | --- | --- |
| 50Hz | 8 | LI 4/8 | 8 bytes | Hybrid, Wide |
| 100Hz Full | 7 | LI 4/8 | 13 bytes | 8ch, 12ch, 16ch |
| 150Hz | 7 | LI 4/8 | 8 bytes | Hybrid, Wide |
| 250Hz | 6 | LI 4/8 | 8 bytes | Hybrid, Wide |
| 333Hz Full | 5 | LI 4/8 | 13 bytes | 8ch, 12ch, 16ch |
| 500Hz | 5 | LI 4/6 | 8 bytes | Hybrid, Wide |

100/333 automatically select the Full PHY profile, but you must also select a
compatible switch mode. Incompatible combinations fail before opening the
radio. There is no automatic rate or switch-mode discovery. 8ch and 12ch share
one sync encoding bit, so they cannot be distinguished from sync alone; match
the transmitter's setting explicitly. A wrong setting can otherwise interpret
a low-group packet identically before a high-group packet exposes the mismatch.

## Live channel dashboard

Close GQRX or anything else using the HackRF. Turn on your transmitter in the
supported mode, at low power, with a few metres of antenna separation:

```bash
cd ~/elrs-monitor
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui \
  --model-match on --model-id 11
```

The dashboard shows:

- Hybrid/Wide: CH1–4 raw 10-bit values, CH5/AUX1 armed flag, and separately
  remembered round-robin AUX2–8. Wide uses six-bit AUX values and skips the
  nonce-selected TX-power slot rather than mislabeling it as a channel.
- Full modes: all 8/12/16 raw 10-bit channels, preserving unseen/stale groups
  independently. **CH5 is a real 10-bit channel in Full modes**, not replaced
  with the armed bit. The separate armed flag is shown in the header.
- Channel level bars, observed min/max, sample age, selected profile, and Model
  Match setting. Raw ranges in standard and Full modes represent different
  CRSF spans; they are not converted to pulse widths.
- `UNSEEN` for channels never observed, `STALE` when sample age exceeds one
  second, and `HELD` for recent retained values without active lock or at EOF.
- Sync lock, decoded frame counts, input progress, **DSP behind** (sample-time
  backlog, including lookahead/queued work), last RF/CFO, output RC/s, queue
  occupancy, and application-buffer drops.
- **Decoded RC gap last/max**: spacing between validated RC packets on the IQ
  sample clock, not repaint timing or a count of all missed RF packets.

**Controls**

| Key | Action |
| --- | --- |
| `q`, `Esc`, `Ctrl-C` | Stop acquisition and exit; restore the terminal |
| `Space` | Pause/resume display only; decoding and logging continue |
| `r` | Reset observed min/max; preserve current readings |
| Up/Down, PageUp/PageDown, Home/End | Scroll channel rows (e.g. 16ch in a short terminal) |
| Resize terminal | Redraw automatically; minimum 80 columns × 22 rows |

On replay EOF, a finite capture finishing, or an error, the final dashboard
stays visible until `q`. Errors are also printed after the terminal is restored
and produce a nonzero exit code. `--seconds` limits acquisition, not the time
for which the final dashboard remains displayed.

Values are **raw OTA units, not microseconds**. For Hybrid AUX2–7, codes 0–5
represent six positions and code 7 is center (`M`); code 6 is reserved and is
also treated as center by the receiver. Their bars show position indices, not
calibrated pulse widths. AUX8 uses four-bit values 0–15. Unobserved AUX values
are never filled in with zero. Wide AUX2–8 are six-bit values 0–63. Standard
Hybrid/Wide modes show CH1–12; Full modes show exactly 8, 12, or 16 channels.
Only channels actually present in a packet are updated. In particular, a high
16ch group does not refresh CH1–8 or overwrite them with invented zeros.

Age uses the received sample clock, including backlog; it advances through
input stalls and freezes at EOF. The explicit STOPPED/EOF banner distinguishes
retained values from a live link. Fast unpaced replay may make values appear
stale due to read-ahead; use `--pace` for a realistic display.

### Why 50Hz does not mean a 20ms dashboard control stream

The transmitter hops across 80 channels, but this fixed-band capture covers
only nine. It misses packets outside that slice, and some slots are used for
sync/data/telemetry rather than RC. The 20-second physical 50Hz/Wide recording
contains 138 decoded RC packets (6.9/s), with sample-time RC gaps from 20ms to
960ms. In the binding-to-monitoring capture, the longest decoded RC gap is
1.02s. These are measured gaps, not guarantees for other conditions.

The TUI repaints every 100ms, and the block/overlap pipeline adds latency.
Use `DSP behind`, queue occupancy and `app drops` to identify software backlog;
compare those with decoded RC gap and channel sample age. Application drops do
not account for all possible USB or over-the-air losses. A low backlog does
not rule out demodulation misses. Observed 2.5-second delays need live metrics;
we cannot attribute every delay to hopping from these recordings alone.
Unpaced replay uses input backpressure and can read ahead; zero replay drops
alone do not prove there are no live overruns. Use live or paced-replay metrics
to assess real-time backlog. Faster repainting cannot recover packets outside
the captured band.

## Replay the existing recording

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui \
  --model-match on --model-id 11 --replay ~/elrs-250hz-hybrid.iq --pace
```

Input must be **signed int8 interleaved I/Q, 10 Msps, center 2440 MHz** with no
header. Files from other settings are not autodetected. No recording is bundled
with the project. Remove `--pace` to benchmark faster than real-time.

## Text, JSON, and logs

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE'  # live human-readable frames
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --format json
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui --jsonl frames.jsonl
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --seconds 10 --radio-diagnostics
# Add --model-match on --model-id 11 for your current transmitter.
```

Normal monitoring emits only known-link CRC-valid frames; binding discovery
emits explicitly labeled candidate/confirmed records. Live status shows PHY
candidates and CRC/FEC/identity/mode rejection counts. Diagnostic/status output goes to
stderr in text mode, or to the event area in TUI mode; it does not corrupt JSON
stdout. `--jsonl` works in all modes, uses owner-only permissions, and exclusively
creates a **new** file rather than overwriting an old one. The binding phrase
and derived UID are displayed normally; neither is a cryptographic secret.

Useful options:

- `--workers 3`: more parallel DSP on a busy computer (default 2).
- `--offset -600000 400000 1400000`: process just three neighboring channels,
  retaining the designated sync channel; useful if CPU load is too high.
- `--stale-after 2`: change the TUI stale-sample age threshold.
- `--lna 16 --vga 16`: conservative default RX gains; RF amplifier and antenna
  bias power are always disabled by the program.
- `--serial SERIAL`: select a HackRF if more than one is attached.

## How it works and what it cannot do

1. `hackrf_transfer` supplies IQ to a bounded input queue, or replay supplies
   samples from a file. The program never passes TX flags to the radio utility.
2. Overlapped 100 ms cores are FFT-channelized onto nine channels centered at
   2436.4–2444.4 MHz within the fixed 2440 MHz / 10 Msps capture.
3. The selected SF5–8 chirps, preamble, sync word, timing, and carrier offset
   are recovered. Symbol lengths, delimiter extras, and packet airtime are
   profile-specific; SF7+ first-block protection is handled separately.
4. Long interleaving, FEC, and whitening are reversed. 500Hz uses punctured 4/6
   coding: errors are detected but the decoder does not claim single-bit
   correction. 4/8 profiles allow unambiguous single-bit correction.
5. Sync must pass UID-derived CRC14 (8-byte) or CRC16 (13-byte) and identity
   checks, plus rate/switch/protocol checks.
6. Non-sync nonces are inferred from validated sync anchors and the firmware's
   exact interval (including 6666us and 3003us), not a rounded Hz division.
   CRC-valid uplink packets yield explicit per-channel updates.

Expected processing/display latency is a few hundred milliseconds; initial
acquisition can wait longer for sync. The six-block raw queue bounds backlog
at about 600 ms. When it overflows, old blocks are discarded, the gap is reported,
a boundary window is dropped, and sync is reset. Nonce state expires after at
least two seconds; slower profiles allow 1.5 times the expected 80-channel hop
cycle so their longer sync gaps do not prematurely discard lock. Overlap grows
for packets longer than 10ms (50Hz). These limits are visible rather than
displaying old values as live controls.

**Limitations:**

- Partial hopping-band coverage: sparse updates, missing packets, no universal
  reception or complete control stream. No fast hardware hop following yet.
- `app drops` counts application-queue loss, **not every possible USB/firmware
  loss**. Underlying sample discontinuity can require reacquisition.
- CRC is error detection, not cryptographic authentication. The binding phrase
  is not ordinary payload encryption.
- No automatic rate/switch discovery or UID discovery from normal traffic.
  Binding discovery requires an explicit transmitter Bind session.
- No FLRC, Gemini, telemetry interpretation, or data-payload reassembly.
- No command/transmit path, firmware flashing, receiver binding, or failsafe
  control. The dashboard is an instrument, not a flight-control receiver.
- Use low transmitter power and antenna separation. Never directly connect a
  transmitter RF output to the HackRF without appropriate attenuation.

## Development and tests

```bash
.venv/bin/python -m unittest discover -s tests -v
# Or without installing (with dependencies already available):
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The package layout separates RF/DSP, protocol validation, streaming lifecycle,
state tracking, and terminal presentation:

```text
src/elrs_monitor/
  cli.py          argument parsing, text/JSON presentation
  capture.py      HackRF/replay source, bounded queue, overlap windows
  engine.py       presentation-independent streaming pipeline
  profiles.py     LoRa geometry, intervals, mode compatibility
  dsp.py, phy.py  profile-driven channelization and symbol recovery
  protocol.py     FEC, dewhitening, CRC, packet parsing
  decoder.py      known-phrase/UID sync and nonce validation
  binding.py      passive repeated-message binding UID discovery
  model.py        thread-safe channel history and freshness
  tui.py          main-thread curses renderer and controls
```

Core tests include real captured-packet regression and synthetic waveform
boundary crossing; state/TUI tests cover unknown channels, auxiliary mapping,
independent staleness, min/max, terminal sizes, scrolling, and cancellation.
Tests also cover every profile with synthetic waveforms and IQ crossing core
boundaries, all compatible switch modes, CRC14/CRC16, Wide nonce/power slots,
Model Match on/off/IDs, mutually exclusive CLI identities, UID formats,
binding confirmation/expiry/gap handling, documented defaults, rejection of
removed config flags, absence of settings-file access, and correct identity
reporting without mixing startup text into JSON frame output.
Binding tests include a CRC fixture calculated with the firmware's C++ CRC
implementation, synthetic IQ discovery, direct-UID normal 50Hz replay, and a
real curses session retaining and printing a discovered UID.
Synthetic encoder/decoder agreement does **not** independently verify the
remaining inferred long-interleaver extensions; more physical captures are required.
Protocol tests use deterministic packet fixtures and example phrases, with no
external settings required.

Reference material (not vendored dependencies):

- [ExpressLRS 4.1.0](https://github.com/ExpressLRS/ExpressLRS/tree/4.1.0),
  commit `a9d4a9cb5b5687c4c9d7e9e7fbdf44ad93651da6`: OTA, CRC, rate configuration,
  Model Match, phrase-to-UID conversion, and Hybrid switch semantics.
- [gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr), commit
  `862746dd1cf635c9c8a4bfbaa2c3a0ec3a5306c9`: LoRa coding/whitening conventions.
- Semtech SX1280/SX1281 datasheet Rev 3.2: SF5/6 and long-interleaved packet timing.
