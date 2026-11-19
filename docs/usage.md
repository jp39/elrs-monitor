# Usage guide

Run examples from the repository root after installing the package. For an
overview and quick start, see the [README](../README.md). Full CLI reference:

```bash
.venv/bin/elrs-monitor --help
```

## Identity and Model Match

Choose exactly one identity on every invocation:

- `--bind-phrase PHRASE` (alias `--binding-phrase`): derive the UID from a phrase.
- `--uid HEX`: supply four or six hexadecimal bytes, compact, colon-separated,
  or hyphen-separated. Four-byte inputs are padded with `00:00`; the final
  four bytes must not all be zero.
- `--discover-binding`: learn the effective link UID during an explicit
  transmitter Bind session, then continue monitoring.

Model Match is **off by default**. When enabled on the transmitter, use
`--model-match on --model-id ID` with ID 0–63. Setting the ID alone does not enable
matching. Bare `--model-match` enables it; `--no-model-match` disables it.
Model Match settings are strict checks, not automatically detected. ID63's
on-air identity is indistinguishable from Model Match off.

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' \
  --model-match on --model-id 11 --tui

.venv/bin/elrs-monitor --uid 00:00:12:34:56:78 --tui
```

The phrase is a deterministic identity seed, not a secret, authentication key,
or encryption mechanism. Startup/status output includes the supplied phrase,
base UID, link suffix and Model Match-adjusted sync identity. With `--uid`, use
the **base UID**, not the adjusted sync bytes. Frame JSONL logs contain packet
records, not a separate settings dump.

All settings are CLI-only: no configuration files are loaded or saved.

## Passive binding discovery

Start the monitor **before** putting one transmitter into Bind mode:

```bash
.venv/bin/elrs-monitor --discover-binding --model-match on --model-id 0 --tui
```

Discovery listens at 50Hz on 2440.4MHz and requires three distinct matching
CRC-valid binding messages within two seconds. Duplicate observations do not
count; IQ gaps reset confirmations. Only one binding transmitter should be active.
Ordinary 50Hz operation is **not** Bind mode and does not disclose a UID.

Binding reveals UID[2:6], the four link bytes; the displayed `00:00` prefix is
reconstructed. Discovery does not recover the original phrase or full UID,
and does not learn Model Match settings.

After confirmation, capture continues with the same sample clock and log.
A matching normal sync is required before channel output. The learned UID stays
in memory across reacquisition but is not saved to a settings file. There is
no discovery-only mode or implicit time limit. `--seconds` limits the entire
session, including discovery; reaching EOF/time limit before learning a UID
is an error. Learning a UID without subsequently receiving normal sync does
not imply channels are live.

## Rates and switch modes

| Rate | SF | FEC | Packet | Switch modes |
| --- | --- | --- | --- | --- |
| 50Hz | 8 | LI 4/8 | 8 bytes | Hybrid, Wide |
| 100Hz Full | 7 | LI 4/8 | 13 bytes | 8ch, 12ch, 16ch |
| 150Hz | 7 | LI 4/8 | 8 bytes | Hybrid, Wide |
| 250Hz | 6 | LI 4/8 | 8 bytes | Hybrid, Wide |
| 333Hz Full | 5 | LI 4/8 | 13 bytes | 8ch, 12ch, 16ch |
| 500Hz | 5 | LI 4/6 | 8 bytes | Hybrid, Wide |

`--packet-rate`, `--refresh-rate`, and `--rate` are aliases. Numbers or names
such as `'333Hz Full'` are accepted. The initial rate/mode pair must be compatible:

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --rate 150 --switch-mode Wide --tui
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --rate 333 --switch-mode 16ch --tui
```

Live monitoring uses these settings as startup hints and searches all six PHYs
during acquisition. A validated announcement of another rate clears the old
anchor; only matching sync on the destination PHY resumes RC. Same-PHY layout
changes require a second distinct matching sync. Missed announcements can
recover through reacquisition; transitions can take seconds and lose packets.
Identity and Model Match never adapt silently. Unsupported advertised modes
produce an explicit unsupported state, not guessed RC.

Full 8ch and 12ch share a sync encoding. Live uses the safe common **8/12ch**
label; CH9–12 stay UNSEEN until actually carried in valid high-group packets.
Full16ch and standard Hybrid/Wide have distinct encodings. Rate/layout changes
reset channel/arm/range and telemetry history.

## Dashboard

Close other software using the SDR. Minimum terminal size is **80 × 22**;
wide terminals add separate receiver-telemetry and monitor-diagnostics panels.
UTF-8/256-color, ASCII, eight-color and monochrome rendering are supported.

| Key | Action |
| --- | --- |
| `q`, `Esc`, `Ctrl-C` | Stop acquisition and exit |
| `Space` | Pause display only; decoding/logging continue |
| `r` | Reset observed min/max |
| Arrows, PageUp/PageDown, Home/End | Scroll channel rows |

At EOF, a duration limit or an error, the final dashboard stays visible until
quit. Errors are printed after restoring the terminal and return a nonzero exit
code. `--seconds` limits acquisition, not how long the final screen stays visible.

### Channel values and freshness

Values are **raw OTA units, not microseconds**:

- Hybrid/Wide: CH1–4 are 10-bit analog values; CH5/AUX1 is armed. Round-robin
  auxiliary updates are remembered independently. Hybrid AUX2–7 use 3-bit
  position codes (6/7 are treated as center); AUX8 uses 4 bits. Wide auxiliary
  values use 6 bits; TX-power slots are not mislabeled as channels.
- Full: channels are 10-bit values, including CH5. Armed is shown separately.
  A high-group packet does not refresh absent low-group channels.
- `UNSEEN` means never observed, `STALE` means older than the configured threshold
  (default one second), and `HELD` means retained without active live input/lock
  or while paused. Missing values are never filled with invented zeros.

Age follows the sample clock, advances through input stalls, and freezes at EOF.
Unpaced replay can make read-ahead samples appear stale; use `--pace` for a
real-time presentation. The dashboard repaints every 100ms; this is not a
stick-to-display latency guarantee.

### Diagnostics and receiver telemetry

`DSP behind` measures sample-time backlog, including lookahead/queued work.
`app drops` counts application-queue loss, not every USB, firmware or over-the-air
loss. Decoded RC gap is spacing between accepted RC packets on the sample clock,
not repaint timing or a count of all missed packets. Retune-excluded IQ is
reported separately. Low backlog does not imply loss-free demodulation.

Receiver statistics are separate from monitor diagnostics and have independent
freshness/age. RSSI2 may be unavailable on a single-antenna receiver and is shown
as `--`, not 0dBm. Statistics require a connected receiver and telemetry enabled;
the effective ratio and Normal/MAVLink mode come from validated sync. Telemetry
off is displayed explicitly. No battery/GPS/sensor or MAVLink message reassembly
is implemented; forwarded transport payloads remain raw fragments. Downlinks
never update RC or steer the uplink clock.

Timing calibration is hardware-dependent; see the [timing audit](telemetry-timing.md).
Physical results and validation scope are in [validation.md](validation.md).

## Backends and useful options

Native libhackrf is the default. `--backend soapy` explicitly selects the optional
SoapySDR transport. There is no automatic failover. See [soapy.md](soapy.md) for
installation, supported stream formats, named gains and discontinuity handling.

| Option | Purpose / default |
| --- | --- |
| `--serial SERIAL` | Select a device with either backend |
| `--device ARGS` | Soapy-only selector; default `driver=hackrf` |
| `--gain NAME=DB` | Repeatable Soapy RX stage gain; AMP must remain zero |
| `--lna 16 --vga 16` | Default HackRF RX gains; RF amp and antenna bias off |
| `--workers 2` | DSP worker count, 1–8 |
| `--stale-after 1` | Channel stale threshold in seconds |
| `--seconds N` | Limit total session sample time |
| `--radio-diagnostics` | Additional control-call timing |

Live reception follows hops; it does not capture all 80 carriers simultaneously.
Different frequency epochs and missing sample spans are not stitched together.
Host-timed tuning and the conservative 4ms quarantine can exclude packets.
No full-rate reception, universal hardware timing or latency guarantee is made.

## Text output and logging

Omit `--tui` for human-readable frames, or use `--format json` for JSON stdout:

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --format json
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --tui --jsonl frames.jsonl
```

Diagnostic/status output uses stderr in headless mode or EVENTS in the TUI,
not JSON stdout. `--jsonl` creates a **new owner-only file** and never overwrites
an existing one. Discovery records are labeled candidate/confirmed, not RC or
sync lock. Downlinks carry `direction: "downlink"`, `status: "validated_downlink"`,
nonce, mode, ratio and a separate `telemetry` object. Data fragments include raw
payload, package index and acknowledgement rather than decoded sensor values.

## Legacy IQ replay

```bash
.venv/bin/elrs-monitor --bind-phrase 'YOUR PHRASE' --replay capture.iq \
  --rate 250 --switch-mode Hybrid --pace --tui
```

Input is headerless **signed-int8 interleaved IQ, 10Msps, center 2440MHz**.
No recording is bundled. Replay has explicit fixed profile/mode settings and
cannot reproduce live retune timing or adaptive rate changes. Remove `--pace`
to process faster than real time. `--offset` selects a subset of the nine fixed
recording-band channels; live reception does not use it. During binding replay,
a custom offset selection must include the sync channel at `400000`Hz.
