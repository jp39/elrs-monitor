# ELRS Monitor

A standalone, **receive-only** HackRF ExpressLRS decoder with a live channel
TUI, text/JSON output, frame logging, and deterministic IQ replay.
**Native libhackrf remains the default.** An optional **SoapySDR** backend can
use the same HackRF; other devices are not yet RF-validated. See
[backend selection and installation](docs/soapy.md).

![ELRS Monitor live channel dashboard demo](elrs-demo.gif)

**Target:** ExpressLRS 4.1.0 / OTA-v4, **2.4 GHz LoRa**, non-Gemini. Six
LoRa profiles and all corresponding switch modes are configurable. **250Hz /
Hybrid and 50Hz / Wide have physical IQ recordings. All six PHY profiles now
have physical packet regressions, including receiver link statistics. Full switch-mode
coverage and long-run reception yield remain experimental.**
**Live capture now follows the transmitter's 80-channel hopping sequence.**
It acquires a normal sync on 2440.4MHz, then retunes automatically. This is
experimental: retune gaps, OS timing, and RF conditions can still miss packets;
it is not a guaranteed complete control stream. Legacy raw IQ replay remains
fixed-band. FLRC/GFSK modes such as D250 and F1000 are deliberately excluded.

## Automatic live rate and switch adaptation

Live monitoring treats `--rate` and `--switch-mode` as **startup hints**, not fixed
restrictions. A known phrase/UID and the configured Model Match checks are still
required. The same command can remain running while you change among the six
supported 2.4GHz LoRa packet rates and their supported switch encodings.

Acquisition/reacquisition searches supported PHYs on 2440.4MHz. The profile bank
shares one channelization pass; steady-state monitoring decodes only the active
PHY. An identity/CRC/frequency-valid sync announcing another rate enters
**RECONFIGURING LINK**. Announcements may use the old PHY: they are not used as
new-profile hop anchors. Only matching sync decoded on the destination PHY can
resume RC. Same-PHY switch-layout changes require a second distinct matching sync.
If an announcement is missed, loss of hop lock returns to profile-bank acquisition.
The source, identity, sample clock and frame log remain open across changes.

The TUI updates rate, switch label and channel layout automatically. Rate/layout
changes reset channel/arm/range and telemetry history rather than reinterpreting
old values under the new layout. JSON frames/status include active `packet_rate`,
`switch_mode`, `switch_mode_label`, `configuration_epoch`, and `adaptive`;
proposal events use `announced_change` or `unsupported_change` with a reason.

**Full 8ch and 12ch share a sync bit.** Live display therefore labels the safe
common layout **8/12ch**, with CH9–12 UNSEEN until explicitly carried by a
CRC/nonce/frequency-valid high-group packet. It never claims 8ch merely because
high-group packets were not received. Full 16ch and standard Hybrid/Wide have
distinct sync encodings. Legacy replay still uses the explicit rate/mode and
does not scan or dynamically reconfigure.

Unsupported advertised rates (including FLRC), Gemini and reserved link modes
produce an **UNSUPPORTED MODE** state, not RC output. Model Match/model ID checks
are never silently changed; nor can this follow an unknown new binding UID.
No settings file is read or saved. Initial CLI hints must still form a valid pair.

Validation: a deliberately wrong 250Hz/Hybrid live hint acquired the actual
50Hz/Wide link: **497 RC / 35 receiver stats / 2 sync in 15s**, zero host drops
or reacquisitions. A synthesized-IQ 50Hz/Wide → 250Hz/Hybrid transition passes
through the real live engine/DSP and updates the model layout. **An actual
transmitter rate-change handover still needs RF validation.** Transitions can
lose packets and take seconds to reacquire; no seamless/full-rate guarantee.
Receiver telemetry has physical packet regressions at all six supported rates.
The user's live rate sweep was captured and rechecked with the corrected decoder;
this is not a second post-fix live sweep or a guarantee across every receiver board.

## Receiver telemetry

Receiver link statistics are decoded automatically after a matching normal sync:
receiver-reported **uplink RSSI1/RSSI2, SNR, link quality, active antenna,
Model Match and true-diversity flags**. Link Mode (Normal/MAVLink) and the
**effective telemetry ratio** are read from sync and followed dynamically;
there are no manual mode/ratio flags. Telemetry off is displayed as `off`.

Use the usual command, with the bound transmitter **and receiver connected**
and transmitter telemetry enabled:

```bash
.venv/bin/elrs-monitor --bind-phrase julian --rate 50 --switch-mode Wide \
  --model-match on --model-id 0 --tui
```

The compact dashboard has an RX statistics line; the wide layout separates
**RECEIVER TELEMETRY** from **MONITOR DIAGNOSTICS**. Receiver statistics have
independent UNSEEN/LIVE/HELD/STALE states and sample age. The stale threshold is
at least `--stale-after`, or three announced telemetry periods if longer.
RSSI2 may be unavailable on a single-antenna receiver; zero/unavailable RSSI
is shown as `--`, not 0dBm. These are receiver reports, **not HackRF RF estimates**.
A bare receiver does not automatically provide battery/GPS/flight-controller data.

Text labels downlinks `RX LINKSTATS` or `RX DATA`. JSON/JSONL downlinks carry
`direction: "downlink"`, `status: "validated_downlink"`, inferred nonce, mode,
ratio and a separate `telemetry` object. Linkstats use `kind: "link_statistics"`;
forwarded data remains `kind: "data_fragment"` with package index, acknowledgement
bit and raw `payload_hex`. **No CRSF sensor or MAVLink message reassembly yet.**
MAVLink mode detection and common link-statistics decoding are not full MAVLink
payload support. Telemetry never updates RC channels or advances the uplink clock.

Direction is gated by the RX TOCK timing, announced telemetry slot, identity-seeded
CRC/nonce and (live) predicted FHSS frequency—not packet type alone. Ratio changes
are applied from validated sync; ambiguous transition packets may be discarded
rather than guessed. No telemetry is decoded before sync or across reacquisition.

To clear the unchanged 4ms retune quarantine before telemetry, selected telemetry
hops retune earlier, potentially losing the preceding uplink. Early tuning is
capped at one in four hop boundaries to preserve uplink reception even at 1:2;
standard ratios already satisfy that cap. Other downlinks may remain uncaptured.
`telemetry_early_retunes` counts these decisions. **50Hz/Wide/MM ID0 has a connected
TX+RX live check: 90 link-statistics reports, 1,386 RC and 6 sync in 35 seconds,
zero host drops/reacquisitions, maximum decoded RC gap 80ms.** The 50Hz timing
gate includes the measured ~380–400us RF-start residual relative to the
firmware-only estimate, not an isolated measurement of IRQ/radio latency. **250Hz/Wide/MM ID0:** 65 link-statistics reports,
2,284 RC and 4 sync in 20s, zero host drops/reacquisitions, maximum RC gap 108ms.
Its measured downlink starts ~240us before the uplink grid, not the firmware-only
500us estimate; a +260us RF-start correction gives a narrow ±80us direction gate,
separate from the ±100us uplink gate. Retune scheduling still uses the earlier
firmware estimate and is unchanged. The remaining 100/150/333/500Hz direction
models now have physical regressions from a live rate sweep. Across that recorded
sweep, the corrected decoder accepts **811 receiver reports** while retaining all
**16,583 previously decoded RC packets**. At 500Hz, receiver packets fall inside
the old RC ±100us window: a narrower ±45us uplink gate now keeps directions
separate. No complete telemetry/RC stream is guaranteed.

The firmware scheduling rule is source-derived; the RF-start residual is
**measured calibration**, not an ELRS specification constant or a measurement of
IRQ latency alone. The source/datasheet trace, timing equation, measured ranges,
and hardware limitations are documented in [the timing audit](docs/telemetry-timing.md).
Other receiver hardware/firmware, custom ratios and MAVLink still need validation.

## Install

Requires Linux/Unix, Python **3.11+**, NumPy, SciPy, and (for default RF reception)
the **libhackrf** host library (loaded directly using ctypes). The optional
`--backend soapy` requires SoapySDR ABI 0.8 and its device plugin instead; no Python
SoapySDR bindings or GNU Radio are required. The TUI uses Python's standard `curses` module; there is no
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

## Alternative SoapySDR transport

The existing command still uses native libhackrf. To use SoapySDR instead:

```bash
.venv/bin/elrs-monitor --backend soapy --bind-phrase julian \
  --model-match on --model-id 0 --tui
```

The default Soapy selector is `driver=hackrf`; override it with `--device ARGS`.
`--serial` works with both backends. There is **no automatic mid-stream failover**;
restart with the other backend to establish a new sample clock. Replay is unchanged.

Physical 150Hz/Wide check, 25s per backend: Soapy **2308 RC / 103 receiver stats**,
native **2118 RC / 90 receiver stats**, both with decoded packets on all 80 nominal
channels, zero host queue drops and no reacquisitions. Different acquisition times
mean these totals are not a performance ranking. Other Soapy devices/profiles still
need RF validation. [Dependencies, limitations and complete results](docs/soapy.md).

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
| Packet rate | Live startup hint 250Hz; fixed for legacy replay; binding uses 50Hz |
| Switch mode | Live startup hint Hybrid; fixed for legacy replay |
| Model Match | Off |
| Model ID | 0; ignored while Model Match is off |
| Capture source | Live native HackRF; first available device; optional `--backend soapy` |
| Text output | Human-readable; use `--tui` or `--format json` to change |
| DSP workers | 2 |
| RX LNA / VGA gain | 16 / 16 dB; RF amplifier and antenna bias off |
| Stale threshold | 1 second |
| Acquisition duration | Until stopped/EOF; `--seconds` limits the entire session |
| Replay speed | As fast as possible; use `--pace` for real time |
| JSONL logging / radio diagnostics | Off |
| Live acquisition / monitoring | Sync channel, then automatic FHSS following; no opt-in flag |
| Legacy replay channels | All nine in the fixed recording band; `--offset` is replay-only |

Minimal command for a 250Hz/Hybrid transmitter with Model Match off:

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui
```

For the previously validated 250Hz/Hybrid setting with Model Match **on, ID 11**,
supply those flags explicitly; the old file no longer provides them:

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

`--rate` and `--switch-mode` specify live normal-link startup hints (fixed normal
settings for legacy replay), not binding settings. Live normal sync can adapt
them after Bind ends. Model Match/model ID remain explicit, strict checks.
Defaults remain 250Hz/Hybrid, Model Match off, ID 0. Binding acquisition always
uses 50Hz on 2440.4MHz independently. Incompatible initial hint combinations
are rejected before capture starts.

Do not supply a phrase or UID alongside `--discover-binding`. Live acquisition processes the sync channel only, then follows the learned
link. For replay the default channel selection is all nine; a custom `--offset`
selection must include `400000` during discovery. Replay uses the same signed-int8 10Msps/2440MHz format and supports
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

100/333 select the Full PHY profile; initial hints must include a compatible
switch mode. Incompatible combinations fail before opening the radio. Live
rate/switch adaptation is automatic after validated sync. 8ch and 12ch share
one sync encoding bit, so live uses the safe shared **8/12ch** mapping and label.
For fixed-profile replay, select the transmitter's actual mode explicitly.

## Live channel dashboard

Close GQRX or anything else using the HackRF. Turn on your transmitter in the
supported mode, at low power, with a few metres of antenna separation:

```bash
cd ~/elrs-monitor
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui \
  --model-match on --model-id 11
```

The dashboard uses a dark cyan/violet instrument-panel layout, smooth
fractional-block channel meters, half-scale guides for analog channels, and
an explicit link-state badge. At 116+ columns, a separate receiver-telemetry and
monitor-diagnostics sidebar appears; compact terminals retain the channel table
with scrolling and a receiver-statistics summary.
UTF-8/256-color terminals get the full theme; ASCII, eight-color and monochrome
fallbacks require no extra dependencies. Pausing marks retained readings HELD,
not LIVE. No receiver/DSP behavior is changed by the presentation layer.

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
- **HOP FOLLOWING** / **REACQUIRING SYNC** phases, retune count, and excluded IQ
  duration. `--radio-diagnostics` adds maximum control-call timing. Known
  identities remain displayed during reacquisition.

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

### Update cadence and latency

Hop following replaces the old fixed-band live receiver. It aims to receive
across the full sequence, not all 80 carriers simultaneously. Some slots carry
sync/data/telemetry, and retune uncertainty can still exclude RC packets.
At 50Hz the scheduler uses the idle time after the old hop's last packet to
retune before the next packet. Faster profiles have much less idle time, so
several slots per hop may be lost. A short live 50Hz/Wide/MM ID 0 run received **445 RC packets in 12 seconds**
across all 80 channels, with zero application-queue drops and a maximum decoded
RC gap of 60ms. This does **not** guarantee full-rate reception; other PHYs
and long-running TUI performance still need RF validation. Stable frequency spans are batched into one job per typical 50Hz hop;
acquisition uses at most 40ms cores plus overlap, rather than tiny USB fragments.

For comparison, the old fixed-band 20-second 50Hz/Wide replay yielded 138 RC
packets (6.9/s), with gaps up to 960ms. The new high-duty-cycle detector retains
all of those and finds five more (143 RC). These remain fixed-band recordings;
they do not measure the new follower's live reception rate. Old observations
also include some RF carriers aliased into the 10Msps band.

The TUI repaints every 100ms, and the block/overlap pipeline adds latency.
Use `DSP behind`, queue occupancy and `app drops` to identify software backlog;
compare those with decoded RC gap and channel sample age. Application drops do
not account for all possible USB or over-the-air losses. A low backlog does
not rule out demodulation misses. Observed 2.5-second delays need live metrics;
we cannot attribute every delay to hopping from these recordings alone.
Unpaced replay uses input backpressure and can read ahead; zero replay drops
alone do not prove there are no live overruns. Use live or paced-replay metrics
to assess real-time backlog. Faster repainting cannot recover excluded or missed RF packets.

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
# Supply rate, switch mode, and Model Match flags for the transmitter in use.
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
- `--offset -600000 400000 1400000`: **replay only**, process three neighboring
  channels. Live reception follows the sequence; no fixed-band live mode remains.
- `--stale-after 2`: change the TUI stale-sample age threshold.
- `--lna 16 --vga 16`: conservative default RX gains; RF amplifier and antenna
  bias power are always disabled by the program.
- `--serial SERIAL`: select a device by serial with either backend.
- `--backend native|soapy`: native HackRF by default; SoapySDR is an explicit fallback.
- `--device 'driver=hackrf,...'`: SoapySDR device selector; requires `--backend soapy`.
- `--gain NAME=DB`: repeatable Soapy RX stage gain; AMP must remain 0.

## How it works and what it cannot do

1. Live RX uses libhackrf at 10Msps; only receive APIs are bound. RF amplifier
   and antenna bias are explicitly off. The RX callback drains continuously into
   a bounded queue and keeps an absolute sample counter.
2. Acquisition listens on 2440.4MHz. A UID-derived 240-entry sequence (three
   permutations of 80 channels) and a normal sync establish hop index and nonce.
   An independent timing thread retunes the receiver; batched stable frequency
   spans (bounded 40ms cores plus overlap during acquisition) are channelized at +400kHz from each tune center to avoid DC. Different
   frequency epochs are never stitched together. Replay alone retains 100ms
   fixed-band cores and its nine-channel FFT bank.
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
   CRC-valid uplink packets yield explicit per-channel updates. In live mode,
   the predicted hop frequency must also match. Validated uplinks refresh phase
   without fabricating additional sync packets.

The tuning scheduler uses packet airtime and the firmware's TXdone hop timing.
It leaves 500us after the old payload before an early tune when the profile's
idle guard permits it (about 8.7ms before the next 50Hz hop's first packet).
IQ during the tuning call plus a conservative 4ms uncertainty/settling interval
is excluded and reported separately from queue loss. These host-time boundaries
are estimates: HackRF does not provide hardware sample timestamps here.

After at least 500ms without validated packets (or six hop periods, whichever
is longer), live reception returns to sync acquisition. Known application-queue gaps do **not** discard the hop clock: the native
callback counter still includes those samples. Windows never stitch across
the gap, and tracking continues only while CRC/grid/frequency validation stays
current. Unknown loss or validation timeout still triggers reacquisition.
The known UID remains in memory. The host/sample timing estimate
is recalibrated after suspected loss; no exact hardware/USB loss count is claimed.
Six queued USB-sized segments bound the raw live queue, rather than the former
six 100ms blocks; pending DSP work adds latency too. Version 0.6.1 batches
frequency spans, uses fast FFT lengths and skips expensive recovery of bursts
belonging to guard regions. TUI footer T/H counts timing-grid / hop-frequency
rejections, useful when following stalls without queue loss. UI refresh remains 100ms.
A short active-transmitter check is recorded above; no stick-to-display latency
or loss-free long-run guarantee is made.

**Limitations:**

- Experimental host-timed hopping, not simultaneous wideband reception. Retune
  exclusions, scheduling misses, and RF interference can lose packets. No
  universal reception or complete control stream is promised.
- `app drops` counts application-queue loss, **not every possible USB/firmware
  loss**. Underlying sample discontinuity can require reacquisition.
- CRC is error detection, not cryptographic authentication. The binding phrase
  is not ordinary payload encryption.
- Live adaptation covers the six supported LoRa profiles, not every transmitter
  parameter. No UID discovery from normal traffic; binding discovery requires
  an explicit transmitter Bind session. Model Match settings remain explicit.
- No FLRC, Gemini, CRSF sensor/MAVLink message decoding, or data-payload reassembly.
  Receiver link-statistics direction has physical packet regressions on all six
  supported PHYs, not universal timing guarantees across hardware/firmware builds.
- No command/transmit path, firmware flashing, receiver binding, or failsafe
  control. The dashboard is an instrument, not a flight-control receiver.
- Use low transmitter power and antenna separation. Never directly connect a
  transmitter RF output to the HackRF without appropriate attenuation.

## Roll back to the pre-hopping implementation

The working 0.5.0 source/tests/docs are committed as **`325e2b7`**, tagged
**`pre-hop-following-0.5.0`**. The fixed-band live implementation is no longer
a selectable runtime mode; use Git to restore it without deleting the new work:

```bash
git switch -c rollback-fixed-band pre-hop-following-0.5.0
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

Restart any running monitor after switching versions. The feature remains on
`master`; `git switch master` returns to it when the working tree permits.
Captures and the ignored old settings file are not changed by these commits.

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
  capture.py      legacy raw replay, bounded queue, overlap windows
  engine.py       entry point and fixed-recording replay pipeline
  hackrf.py       unchanged default native RX transport and retune epoch boundaries
  soapy.py        optional receive-only SoapySDR transport (C API, CS8/CF32)
  fhss.py         firmware-compatible sequence and unwrapped hop/slot clock
  follow.py       validated phase tracking and independent tuning scheduler
  live.py         default live acquisition/following/reacquisition pipeline
  adaptive.py     validated live rate/switch proposals and destination-PHY confirmation
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

## License and attribution

Copyright (C) 2026 ELRS Monitor contributors. The project is licensed under the
**GNU General Public License, version 3 only (GPL-3.0-only)**. See [LICENSE](LICENSE).
It is provided **without warranty**. When distributing modified versions or
binaries, retain the required notices and provide corresponding source as the
GPL requires.

The FHSS implementation includes a Python adaptation of ExpressLRS code;
ExpressLRS and gr-lora_sdr also informed protocol and coding behavior. Their
upstream authors retain their rights. See [third-party notices and provenance](THIRD_PARTY_NOTICES.md)
for attribution and the separate licenses of external runtime libraries/drivers.
This is an independent project, not an official ExpressLRS product.

Reference material (not vendored dependencies):

- [ExpressLRS 4.1.0](https://github.com/ExpressLRS/ExpressLRS/tree/4.1.0),
  commit `a9d4a9cb5b5687c4c9d7e9e7fbdf44ad93651da6`: OTA, CRC, rate configuration,
  Model Match, phrase-to-UID conversion, and Hybrid switch semantics.
- [gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr), commit
  `862746dd1cf635c9c8a4bfbaa2c3a0ec3a5306c9`: LoRa coding/whitening conventions.
- Semtech SX1280/SX1281 datasheet Rev 3.2: SF5/6 and long-interleaved packet timing.
