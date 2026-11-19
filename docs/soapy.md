# Optional SoapySDR backend

## Selection and scope

The default is still **native libhackrf**, with its transport implementation
unchanged. SoapySDR is an explicit alternative/fallback, not automatic failover:

```bash
# Existing native backend; --backend native is optional.
.venv/bin/elrs-monitor --bind-phrase julian --model-match on --model-id 0 --tui

# Same HackRF, through SoapySDR instead.
.venv/bin/elrs-monitor --backend soapy --bind-phrase julian \
  --model-match on --model-id 0 --tui

# Select a particular device.
.venv/bin/elrs-monitor --backend soapy \
  --device 'driver=hackrf,serial=0000000000000000406464c82375594b' \
  --bind-phrase julian --model-match on --model-id 0 --tui
```

`--device` defaults to `driver=hackrf`. `--serial` works with either backend;
do not combine it with a `serial=` entry in `--device`. `--device` and `--gain`
require SoapySDR. Replay never opens either SDR backend and rejects Soapy-only
options. No configuration file is loaded.

The chosen backend is announced at startup. Status JSON includes `backend`,
`radio_driver`, `stream_format` and `stream_overflows`. The latter is `null`
for native HackRF, which does not expose a driver-overflow counter here.

**There is no silent backend switch after a stream failure.** Sample clocks and
retune epochs are local to a session. Stop the old session and start another
with the other backend; do not trust old anchors across a transport restart.

## Dependencies

Native reception still needs only libhackrf. Choosing native does not load
SoapySDR. The optional backend uses the **SoapySDR ABI 0.8 C API** via ctypes;
it does not require Python SoapySDR bindings or GNU Radio. Install the SoapySDR
runtime **and the device's plugin**. A core library without a plugin cannot open
that device. Check:

```bash
SoapySDRUtil --info
SoapySDRUtil --find='driver=hackrf'
SoapySDRUtil --probe='driver=hackrf'
```

On this Fedora machine the core was already installed but no plugins were
present. Development headers were installed with:

```bash
pkexec dnf install SoapySDR-devel hackrf-devel
```

The official [SoapyHackRF](https://github.com/pothosware/SoapyHackRF) plugin was
built at commit `0b8d3f691ed53eae8e9837804a5dcecd313bd15a`:

```bash
git clone https://github.com/pothosware/SoapyHackRF.git
cd SoapyHackRF
git checkout 0b8d3f691ed53eae8e9837804a5dcecd313bd15a
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local
cmake --build build -j4
pkexec cmake --install build
```

Here that installs `/usr/local/lib64/SoapySDR/modules0.8/libHackRFSupport.so`.
It reports plugin version `0.3.6-0b8d3f6`; core is `0.8.1-22.fc44`. Prefer your
system's packaged SoapyHackRF module when available. Source/build artifacts from
this installation are in `/tmp/elrs-soapyhackrf`, not runtime dependencies.

## Receive contract and limitations

- RX channel 0 only. No TX stream/write APIs are bound or called.
- Exact **10Msps** is still required; there is no arbitrary-rate resampler.
  Startup checks the returned sample rate and all 80 tune centers, 2400–2479MHz.
  The requested receive bandwidth is 8MHz, as with native HackRF.
- **CS8** is preferred, preserving HackRF byte values. Drivers exposing only
  **CF32** are supported by finite-value checking and signed-int8 quantization.
  This is not full-bit-depth floating-point DSP. Other formats alone are rejected.
- HackRF retains LNA/VGA defaults of 16/16dB and the existing `--lna`/`--vga`
  controls. For other drivers, use repeatable `--gain NAME=DB` named RX gains.
  Named gains override those defaults where applicable. Automatic gain is
  disabled where supported; overall gain is not used because it can enable AMP.
- An exposed `AMP` gain is forced to zero and read back. Known antenna-bias
  settings (`bias_tx`, `biastee`, `bias_tee`, `bias`, `antenna_enable`, case-insensitive)
  are disabled and read back. Other device-specific controls need review before
  claiming equivalent hardware safety on another model. No bias/amp-enable flag
  is provided.
- Host/sample timing remains an estimate. Optional hardware timestamps are used
  to check continuity, not as a claim of accurately timed RF retunes.
- The existing six-block application queue, known-drop sample accounting,
  independent hopping scheduler and **4ms retune quarantine** remain. Old/new
  frequency epochs are sliced separately; IQ is not stitched across gaps.
- Soapy read timeouts are tolerated subject to the existing source-idle watchdog.
  Driver overflow, abrupt termination and timestamp discontinuity **stop with an
  error** rather than inventing a missing-sample count or keeping a corrupt hop
  clock. Restart the session to reacquire. This is not transparent loss recovery.
- More driver support is not a guarantee of fast-enough tuning. Only HackRF has
  been RF-validated through this backend. A typical RTL-SDR cannot directly tune
  this 2.4GHz link; selecting its driver does not change that limitation.

## Physical HackRF validation

Two sequential **25s** live sessions, same HackRF/antenna and application settings,
with connected transmitter/receiver at **150Hz/Wide**, Model Match ID0,
telemetry **1:32**. Both started with the deliberately mismatched default
250Hz/Hybrid hint and automatically acquired the actual profile:

| Metric | SoapySDR | Native libhackrf |
|---|---:|---:|
| Valid RC packets | 2308 | 2118 |
| Receiver LINKSTATS | 103 | 90 |
| Valid sync packets | 4 | 4 |
| Nominal channels with decoded packets | 80 | 80 |
| Retunes | 858 | 782 |
| First accepted sync, sample seconds | 2.067 | 4.104 |
| Maximum subsequent RC gap | 93.324ms | 40.010ms |
| Host application queue drops | 0 | 0 |
| Reported driver overflows | 0 | unavailable |
| Reacquisitions after acquisition | 0 | 0 |
| Retune-excluded sample time | 3.726s | 3.378s |

Both reported genuine receiver RSSI/SNR/LQ and Model Match. Counts are not a
throughput ranking: acquisition times and RF conditions differ, and neither
receives every transmitter slot. The Soapy validation is at 150Hz/Wide on this
HackRF, **not all profiles, boards, drivers, switch modes or loss conditions**.

Artifacts: `/tmp/elrs-{soapy,native}-connected-live.{jsonl,txt,status}`. Earlier
20s Soapy/8s native checks had clean IQ but no ELRS packets while no matching
signal was observed; those are transport checks, not RF-decoding evidence.

Four focused tests supplement the existing suite: RX settings/cleanup/duration,
CF32 conversion and fail-closed discontinuities, epoch gap/queue accounting, and
startup failure/CLI boundaries. Existing DSP/replay/native tests are retained.
**133 tests pass.**

## Snapshot / rollback

The pre-change source is pinned by annotated tag **`pre-soapysdr-0.8.2`**, commit
**`56b5ea8`**. A separate source archive is also saved at:

`/home/julian/elrs-monitor-snapshots/elrs-monitor-pre-soapysdr-0.8.2.tar.gz`

SHA256: `e7c4f593d2776dd26a9a41faa707d6b089b827f5bf574831466c9b05ad72c083`.

The archive is tracked source, tests and documentation, not IQ captures,
virtual environments or ignored local files. For ordinary fallback, simply omit
`--backend soapy`; no source rollback or plugin uninstall is needed.
