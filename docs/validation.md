# Validation — 0.7.0

## Receiver telemetry implementation (RF validation pending)

Firmware reference: ExpressLRS 4.1.0 `a9d4a9c`, `OTA.h` packed std/full
`data_dl`/`OTA_LinkStats_s`, shared nonce-seeded CRC, `rx_main.cpp`
`HWtimerCallbackTock`, `HandleSendDataDl`, `LinkStatsToOta` and packet-to-TOCK
slack calculation; `tx_main.cpp` telemetry gaps and effective-ratio sync fields;
SX1280 `common.cpp` RFperf TOA and SNR scaling (signed quarter-dB).

Three focused synthetic checks cover all six profiles, Normal/MAVLink metadata,
std/full layouts, nonce wrap, direction gates, wrong CRC/frequency/slot, telemetry
off and sync changes, unchanged RC/anchor state, early tuning, independent age,
compact/wide bounds and paused readings. Full suite: **123 tests pass**. Two
stale renderer assertions in the existing Full-mode scrolling test were updated
to the actual 0.6.2+ row/header format; their old whitespace expectations failed
without indicating a protocol regression.

Physical 20s 50Hz/Wide/MM ID0 IQ replay: **6 sync / 144 RC / 0 downlink**, retaining
all 144 packets in the older `/tmp/elrs-50-validated.jsonl` (6 sync / 138 RC).
Re-encoding and feeding all **449** observations from the validated 0.6.1 live
run through strict tracking retains **445 RC / 4 sync**, without falsely counting
any as telemetry. These are recorded-uplink regressions, not new live validation.

An additional deterministic synthesized-IQ check through channelization, PHY,
FEC, CRC and the real replay CLI recovers sync plus a correctly laid-out downlink,
without creating RC values. A 100us-step scheduler simulation at 1:2 retains
complete uplinks on all six profiles over 290ms (including the 4ms quarantine).
This checks the anti-starvation policy, not real-time scheduling jitter or RF yield.
Script: `/tmp/elrs-telemetry-pipeline-check.py`.

A bounded 12s hardware run produced no PHY candidates or syncs, zero host drops,
and no retunes; it does **not** validate downlink reception or the early-retune
tradeoff. Artifacts: `/tmp/elrs-telemetry-first-live{.jsonl,.txt,-status.log}`;
replay: `/tmp/elrs-telemetry-50-regression.jsonl` and corresponding `.status`.
A connected bound transmitter and receiver with telemetry enabled are required
for the next RF check. No live sensor or MAVLink reassembly is claimed.

Early tuning preserves the 4ms quarantine, but may lose the preceding uplink.
It is limited to one in four hop boundaries (all standard telemetry ratios
satisfy this limit); otherwise an aggressive 1:2 ratio could starve uplink timing
refresh. Retune timing and real telemetry yield still need physical validation.

## Presentation-only refresh

0.6.2 adds the cyan/violet dashboard, fractional-block meters, a wide-terminal
telemetry/events sidebar and monochrome/eight-color/ASCII fallbacks. The live
receiver, DSP, queue and hopping implementations are unchanged from 0.6.1.
Existing state/PTY checks pass; compact/wide, paused and ASCII layouts fit their
terminal bounds. No new test cases were added. Pure layout took about 1.4ms
per 140x26 frame in a local check; this is not end-to-end display latency.
The preview image uses recorded RF, not an active radio capture. Paused values
are marked HELD and stale armed state is not highlighted as currently armed.

## Live-user regression and patch

A user 60-second 50Hz/Wide/MM ID 0 run of 0.6.0 reported 13 syncs, only
105 RC frames, 0.283 seconds of queue drops and repeated overflow-triggered
reacquisition. This confirms reception while hopping but is a performance
regression, not acceptable continuous monitoring. Current backlog being low
at the screenshot does not rule out earlier transient overload.

0.6.1 batches stable RF spans rather than processing tiny retune/USB fragments.
Known software-queue loss no longer invalidates the otherwise continuous
callback sample counter/hop clock. It remains a gap and is never stitched;
CRC/grid/frequency validation and loss-of-lock timeout still apply. FFT lengths
are padded to fast grid-compatible sizes; guard-only bursts avoid repeated
expensive symbol recovery.

Focused before/after execution on the same short deterministic hopping IQ
reduced DSP jobs from 16 to 5 with all six frames retained. Total case runtime
(including synthetic IQ construction) was 0.329s versus 0.213s on this machine;
this is not a live-latency measurement. The existing handover/log check also
passes. No new test cases were added for this patch.

A subsequent **12-second active-transmitter live run** at 50Hz/Wide/MM ID 0
produced **445 RC and 4 sync packets**, with valid frames on **all 80 nominal
hop channels**, **zero application-queue drops**, 249 retunes, and no recorded
reacquisitions or CRC/FEC/timing/frequency rejection counters. Maximum decoded
RC gap was **60ms**. Retune exclusions totaled 1.089s; maximum control call was
1.312ms. Final phase was following. Frame/status artifacts are
`/tmp/elrs-hop-061-live-frames.jsonl` and `/tmp/elrs-hop-061-live-status.log`.

This is a short headless live check, not a guaranteed complete control stream
or a measurement of stick-to-display latency. Long-running TUI reception and
other PHYs remain to validate. Restart existing processes to load the fix.

## Current live hop-following implementation

The pre-change baseline is commit **325e2b7**, tag **pre-hop-following-0.5.0**.
Live capture now uses receive-only libhackrf callbacks and automatically follows
the FHSS sequence after a validated normal sync. The old fixed-band live
subprocess implementation is removed, not selectable by an opt-in/opt-out flag.
Fixed-center raw recording replay remains supported.

The **0.6.0 suite's 120 tests passed**, including all prior protocol/replay regressions
and new checks for:

- All 240 sequence entries against independently compiled ExpressLRS 4.1.0
  `FHSS.cpp` and `random.cpp` (commit a9d4a9c), with OTA-v4 UID seeding.
  Platform/logging stubs do not replace the sequence/RNG implementations.
- Independent 256-slot nonce and 240-hop sequence wrapping, each profile's
  hop interval, TXdone/idle-guard tuning timing, and late-deadline skipping.
- CRC, normal identity/mode sync gating, strict timing grid and predicted RF
  channel checks; validated uplinks refresh phase without inventing syncs.
- Loss-of-lock return to acquisition, scheduler errors and cancellation,
  native RX-only settings, partial-open cleanup and idempotent close.
- Bounded callback queues; malformed/NULL/odd-length transfer rejection.
  Retune epochs have explicit excluded intervals and cannot be stitched.
- Timing recalibration after suspected discontinuity, retaining sample counter.
- Binding-to-normal live handover on one source/log across frequency epochs;
  missing sync never unlocks channels. Existing log paths are not overwritten.
- Synthetic frequency-changing 10Msps IQ through the actual live DSP/decoder,
  including intentionally lost first slots, and a >80%-occupied 250Hz waveform.
- Following/reacquisition labels and separate queue-loss/retune-exclusion metrics.

### Hardware transport checks (not packet-recall proof)

An actual HackRF RX stream completed **79 live frequency changes** at roughly
16ms dwell, with no callback errors or application-queue drops. The maximum
control call was **0.515ms**; excluded IQ totaled **0.341s**, including the
conservative 4ms quarantine per retune. Only RX APIs were used; RF amp and
antenna bias were off. This exercised transport/tuning, not ELRS packet yield.

A separate ten-second 50Hz/Wide/Model Match ID 0 live monitor run completed
cleanly, but detected no ELRS PHY candidates on the acquisition channel.
At that stage no active-transmitter packet check had succeeded. The subsequent
0.6.1 active-transmitter result is recorded above; neither guaranteed full-rate
reception nor stick-to-display latency is claimed. Faster PHYs may lose several slots in retune exclusions.

### Physical fixture and replay checks

All 144 previously validated normal 50Hz packet timestamps/bytes align with
the firmware hop schedule modulo the 10MHz sampling alias. Of the old FFT
carrier labels, 91 match the predicted RF channel directly; 53 differ by exactly
+/-10MHz, consistent with RF aliases in the original partial-band capture.
The new follower checks the actual predicted nominal carrier instead of
treating an aliased fixed-band label as that carrier. A small physical fixture
is included; the 240-entry C++ golden fixture independently validates the port.

The high-duty-cycle floor estimate is now the lower 5%, rather than 20%.
A followed 250Hz carrier exceeds 80% occupancy, where a 20th-percentile floor
can mistake signal for noise. Regression on the physical fixed-band captures:

- **50Hz:** 149 valid frames (6 sync, 143 RC), retaining all previous 144.
- **250Hz:** all 196 valid frames retained (18 sync, 89 RC, 89 data).

These recordings cannot validate retuning, host scheduling, or full-band recall.
Replay remains fixed-center and uses backpressure unless paced.

## Historical fixed-band validation (through 0.5.0)

The following measurements and older test counts describe the saved baseline,
not the new live backend.

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
