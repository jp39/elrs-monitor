# Validation — 0.5.0

The project is independent of the original analysis scripts/reference checkouts.
No IQ recording or local settings file is bundled in distributions.

## Physically recorded RF

The existing ten-second **250Hz / Hybrid / Model Match ID 11** recording now
produces **196 CRC-valid frames: 18 sync, 89 RC, 89 data**, with zero application
buffer drops. Paced replay took **10.10 seconds**, and unpaced processing about
**6.02 seconds** on this machine. A neighboring-preamble-boundary retry recovers
three more RC frames than the preceding version. This is not a calibrated
packet-recall guarantee.

A new twenty-second physical **50Hz / Wide / Model Match ID 0** capture
produces **144 CRC-valid frames: 6 sync and 138 RC**, zero application-buffer
drops. Sticks/switches were largely static; deliberate movement remains to be
checked. The 400MB raw capture is owner-only and Git-ignored under `captures/`.
A fixed SF8 packet-symbol fixture is included in the standalone tests.

This capture exposed a sync-validation bug: `otaProtocol` is the link-mode
enum (`TX_NORMAL_MODE=0`, `TX_MAVLINK_MODE=1`), not the OTA version. The decoder
previously accepted only 1, and all synthetic sync fixtures used 1. Version
0.4.1 accepts both defined modes, rejects reserved values 2/3, and defaults
synthetic fixtures to normal mode. This was a decoder bug, not an RF or
Model Match mismatch. PHY and rejection counters are now visible while searching.

A sixty-second raw capture includes a **real Bind-mode episode** starting at
10.491 seconds. Discovery confirms three identical CRC-valid MSP_ELRS_BIND
messages at **10.491056, 10.531048, and 10.571048 seconds**, yielding
**`00:00:13:ad:5f:d3`**. The four received bytes exactly match the suffix of
`julian`'s base UID (`16:b3:13:ad:5f:d3`). Prefix `00:00` is reconstructed,
not recovered from the original UID. Raw packet: `b5010913ad5fd33f`.

The initial attempt found only one repetition. Bind-mode 50Hz traffic occupies
its fixed channel for >50% of each window; the previous median noise estimator
then measured signal power and hid bursts. Version 0.4.3 uses the lower
quintile instead. A dense alternating DATA/RC synthetic regression and a real
binding-symbol fixture cover this bug. The discovery pipeline confirms all
three messages without any phrase, exits successfully, and emits no RC updates.
Replaying the earlier normal 50Hz and 250Hz captures retains **all 144 and 196
previously validated packets**, respectively, with zero application drops.

## Automatic handover and update cadence

The same sixty-second physical Bind capture now runs without stopping at UID
confirmation. With normal settings 50Hz/Wide/Model Match on/ID 0, it emits
**3 binding records, 14 valid normal syncs, and 289 valid RC packets**. Capture
and its sample clock/log stay continuous. There is exactly one handover, no
application-buffer drops, and normal sync is required before channels update.
The learned UID survives sync timeouts and gaps. End-to-end discovery/handover
is validated on captured RF; live capture orchestration is also mocked in tests.

The original normal 20-second capture delivered 138 RC packets (6.9/s).
Sample-time inter-RC gaps: minimum 20ms, median 60ms, 90th percentile 348ms,
maximum 960ms. The handover capture's maximum decoded RC gap is 1.02s.
These numbers demonstrate sparse reception, not a complete 50Hz stream or an
exact loss percentage. Only nine of 80 hop channels are captured. Sync/data/
telemetry slots and decoder misses also affect RC yield. They do not by
themselves explain every reported 2.5-second live delay.

`processing_lag_seconds` (shown as DSP behind) includes intentional lookahead,
queued work, and processing. `last_rc_gap_seconds`/`max_rc_gap_seconds` measure
validated RC spacing on the source sample clock, independently of wall-time
replay speed. Queue/drop/rejection metrics remain separate; none measures all
USB or RF packet loss. Unpaced replay uses backpressure, so zero drops there
is not a live-overrun test. The UI's 100ms refresh is not the RF sample rate.

A standalone paced replay of the normal 20-second capture finished in 20.22s,
retaining all 138 RC and 6 sync packets, with zero application-buffer drops,
maximum queue depth 1, and maximum DSP backlog 300ms. After the first sync,
RC packet-timestamp-to-output-callback delay was median **346ms**, maximum
**397ms**. This includes block/lookahead, DSP, and dispatch, not just drawing;
the UI can add up to another 100ms. Initial acquisition buffered 15 RC frames
until the first valid sync at sample time 2.464s; the oldest had 2.70s output
delay. These measured software delays have not been optimized in this release.
Concurrent tests increased the observed backlog; these are machine/run-specific
measurements, not latency guarantees or proof of loss-free live RF.

## Automated checks

**90 tests pass**, including:

- Real curses pseudo-terminal sessions: replay EOF, pause, min/max reset,
  resizing/quit, plus binding-to-channel-view handover and retained normal
  channel results at EOF/terminal restore.
- Every configured profile with synthetic I/Q crossing processing-core boundaries,
  plus symbol recovery under both IQ polarities and carrier offset.
- 8/13-byte FEC/dewhitening, CRC14/CRC16, configured-rate nonce timing, and
  rejection of incompatible mode/rate combinations and non-LoRa mode names.
- Wide nonce-dependent AUX slots and the TX-power slot; 8ch/12ch/16ch group
  mappings without filling missing channels; TUI scrolling and staleness.
- Model Match off/on and IDs 0, 1, 11, 63. ID 63's XOR mask is zero, making
  its identity indistinguishable from matching off.
- CLI-only synthetic IQ replay in 150Hz/Wide and 333Hz Full/16ch, including
  JSON logging and startup phrase/base-UID reporting on stderr, not JSON stdout.
- CLI-only settings with sensible defaults and exactly one of phrase, UID,
  or binding discovery;
  removed config flags are rejected, settings files are not read/discovered,
  and help documents defaults, compatible modes, examples, and identity seeds.
- Direct four/six-byte UID formats and normal 50Hz synthetic replay using
  UID instead of phrase; equivalent CRC/identity handling and Model Match.
- Synthetic binding IQ through the complete CLI/JSONL pipeline: three valid
  repeated messages confirm a UID without sync or RC; incomplete discovery
  returns failure. Candidates expire, are bounded, and reset on IQ gaps.
- Rejection of wrong binding command, package index, CRC, nonce, channel,
  duplicates, zero UID, and ordinary normal-link 50Hz packets.
- A fixed binding packet CRC independently calculated by compiling ELRS 4.1.0
  Crc2Byte::calc. Input 01 01 09 12 34 56 78, seed=4, yields CRC14=0x00dd.
  This verifies OTA CRC/layout, not the actual modem's LoRa coding.
- Same-profile and different-profile synthetic handovers (50Hz, 250Hz,
  333Hz Full), including queued jobs and the confirming 100ms core. No normal
  packet is interpreted using binding symbol geometry or vice versa.
- Wrong normal Model Match/switch settings or missing normal sync cannot
  unlock channels. Pre-discovery normal packets are not used as known-link data.
- Exclusive owner-only log creation, cancellation, bounded raw queues, and
  a mocked live child that remains running after UID learning; IQ gaps preserve
  that UID while resetting nonce/sync state.
- RC-gap and DSP-backlog metrics, source-clock timing, and their separate TUI labels.

The 0.5.0 wheel builds and includes the profile and binding modules, but does not include
`config.local.json`, IQ recordings, or the analysis directory. The user's
old configuration remains untouched and Git-ignored, but is no longer used
by the application. Binding phrases are not treated as secrets.

Startup and TUI identity display distinguish the full base UID from its
four-byte link suffix and Model Match-adjusted sync bytes. For the example
phrase `julian`, the base UID is `16:b3:13:ad:5f:d3`; Model Match ID 0 produces
sync bytes `5f:ec` without changing that base UID. Tests cover visibility at
80x22, persistence after events/EOF, explicit UID input, and JSON separation.

## Experimental extensions

**250Hz / Hybrid and 50Hz / Wide have independently captured RF fixtures.**
Other PHY profiles use firmware rate/geometry tables, datasheet
implicit-header timing, and an inferred generalization of the observed long
interleaver. In particular, punctured 4/6 coding and SF7 profiles still need
validation against Semtech-generated RF packets. SF8 has now been checked. Synthetic
encoder/decoder agreement proves implementation consistency, not compatibility
with every on-air modem detail.

**Binding discovery now has a physically captured RF fixture.** The real
50Hz Bind-mode episode validates its CRC/nonce rules, package index, command,
UID layout, repetition confirmation, and high-duty-cycle detection. Ordinary
50Hz operation still does not disclose binding messages.

Wide and full-resolution channel semantics follow the ELRS 4.1.0 OTA source,
but full-resolution switch modes still need physical captures. Other PHY profiles
emit an experimental-PHY warning when started. No non-LoRa mode is enabled.

These checks do not establish universal packet reception or performance on
other machines. Normal 50Hz live reception was also confirmed by the user.
Binding discovery and automatic handover are validated by replaying a
hardware capture; continuous live-child lifecycle is covered by mocks. The
user also confirmed live binding discovery in the preceding stop-after-UID
version. Other PHY modes still need deliberate physical tests.
