# Telemetry timing: source audit and RF reference points

## What is derived, and what is measured

The nonce, telemetry-slot rule, packet layout, CRC and FHSS schedule come from
ELRS 4.1.0 source. The firmware-only timing estimate also comes from source.
**The RF-start residuals are empirical calibration for the tested SX1280 setup,
not constants specified by ELRS or Semtech.** They are not measurements of IRQ
latency alone, and are not promised to fit every receiver/MCU/firmware build.

Reference firmware: ExpressLRS commit
`a9d4a9cb5b5687c4c9d7e9e7fbdf44ad93651da6`, preserved locally at
`/home/julian/elrs-analysis/ExpressLRS`.

## 1. The actual receiver reference is a software callback

Trace in the firmware:

1. `src/lib/SX1280Driver/SX1280.cpp`, `IsrCallback`: reads IRQ status.
2. `RXnbISR`: queries the receive buffer address and reads its payload over SPI.
3. `RXdoneCallback` calls `src/src/rx_main.cpp`, `ProcessRFPacket`.
4. **Only here** does `beginProcessing = micros()` timestamp the packet.
5. After CRC validation, the receiver passes this timestamp plus slack to its
   phase detector:

   ```cpp
   slack = max(interval - 2 * TOA, PACKET_TO_TOCK_SLACK);
   PFDloop.extEvent(beginProcessing + slack);
   ```

   `PACKET_TO_TOCK_SLACK` is 200 microseconds. `TOA` is the firmware's per-rate
   microsecond value in `src/src/common.cpp`, not a hardware RX timestamp.
6. `updatePhaseLock` filters phase error and adjusts the timer. It is a control
   loop, not an immediate exact hardware transition at `beginProcessing+slack`.
7. `HWtimerCallbackTock` records its own phase event, handles RC output, increments
   the nonce, runs `HandleFHSS` and diversity handling, then `HandleSendDataDl`.
8. `HandleSendDataDl` transmits only if telemetry is enabled, the nonce modulo
   telemetry denominator is zero, and connection/model checks permit it.
9. `SX1280Driver::TXnb` enables the RF path, writes the FIFO, and issues `SetTx`.
   Preamble RF transmission begins after the modem's mode transition/ramp-up.

The RX SPI path, timer scheduling/filtering, callbacks before TX, frequency
changes, FIFO write and modem transition all lie between our RF reference and
the firmware's reference. Some are dependent on MCU, board, BUSY pin and build.

`SX1280_hal.cpp::WriteCommand` does **not** simply wait for its `busyDelay`
argument after every command. It records a fallback delay; `WaitOnBusy` polls
the hardware BUSY pin if available, otherwise observes that fallback. Treating
all of those arguments as additive fixed delays would give a false derivation.
`SetTx`'s `100` argument is therefore not an extra guaranteed 100us RF delay.

## 2. Datasheet checks

Semtech SX1280/SX1281 datasheet rev 3.2, March 2020:

- Sections 7.4.3 and 7.4.4.2, pp. 50–52: implicit-header long-interleaved LoRa
  airtime. This is the geometry used by `profiles.py` and `phy.py`.
- Section 10.8 / Table 10-2, p. 70: active mode switching times are **typical**
  values, measured from the end of the SPI command to stable mode/preamble.
  Examples: FS→TX 27us, RX→TX 39us. These do not include MCU IRQ/SPI/callback time.
- Section 11.6.4, p. 79: `SetTx` starts packet transmission after ramp-up.
- Table 11-49, p. 88: programmable PA ramp times. The ELRS driver selects 4us.
  This is not the entire callback-to-preamble path.
- Section 11.9, pp. 95–97: RxDone means receive completion, not the preamble-start
  timestamp used by the passive decoder.

[Datasheet PDF](https://mm.digikey.com/Volume0/opasdata/d220001/medias/docus/483/SX1280-81_Rev3.2_Mar2020.pdf)

There is no exact end-to-end callback-to-RF-start value in these tables. Nor does
ELRS source provide one universal value for its different receiver boards.
Determining individual contributions would require IRQ/SPI/RF instrumentation;
receive-only RF captures establish the combined residual, not its unique cause.

## 3. Derivation of the reference conversion

Let `I` be packet interval, `F` the firmware TOA, `A` actual uplink RF airtime,
`U` the prior uplink preamble start, and `C` its `beginProcessing` callback time.
Let `e` include the phase-loop/timer error and `T` the TOCK-to-downlink-preamble
elapsed time. Then:

```text
S = max(I - 2F, 200us)                      [ELRS source]
C = U + A + receive/IRQ/SPI/callback delay
D ≈ C + S + e + T                          [RF downlink preamble start]
G = U + I                                 [next nonce's uplink grid]
D - G ≈ A + S - I + receive_delay + e + T
```

The original simplified model used:

```text
offset_firmware = F + S - I
```

Its measured residual is therefore a combination:

```text
residual = (A - F) + receive_delay + e + T
           + error in our estimated uplink grid / RF timestamp
```

It is incorrect to label this entire residual “IRQ latency”, or claim it was
read from a protocol specification. `firmware_downlink_offset()` retains the
source-derived estimate for conservative retuning; `downlink_offset()` adds the
explicitly named `MEASURED_RF_START_RESIDUAL_US` for direction classification.
The scheduler and its 4ms IQ quarantine do not use these residuals.

### RF airtime cross-check (microseconds)

| Profile | PHY formula `A` | Firmware table `F` | `A − F` |
|---|---:|---:|---:|
| 50Hz | 10791.385 | 10798 | -6.615 |
| 100Hz Full | 7601.231 | 7605 | -3.769 |
| 150Hz | 5868.308 | 5871 | -2.692 |
| 250Hz | 3328.000 | 3300 | +28.000 |
| 333Hz Full | 2372.923 | 2374 | -1.077 |
| 500Hz | 1506.462 | 1507 | -0.538 |

These small differences, including 250Hz's 28us, cannot alone explain the
observed 200–400us model residuals.

Numerical timestamp audit: `/tmp/elrs-timing-source-audit.py` generated analytic
waveforms at every supported profile, with 0, -3kHz and -115kHz CFO and multiple
coarse start positions. Recovered packets matched and preamble-start errors were
below 0.02us. This checks the estimator under its analytic waveform model, not
independent physical ground truth or every channel/noise impairment. It did not
reproduce a hundreds-of-microseconds timestamp error.

## 4. Calibration and direction safety

Observed offsets below are relative to independently tracked uplink timestamps.
Every selected packet had a matching telemetry nonce, identity-seeded CRC,
predicted hop frequency and genuine LINKSTATS layout. The temporary diagnostic
held these packets out of RC and hop-clock updates, **including near-grid 500Hz
packets**, so their timing was not used to move the reference being measured.

| Profile | Firmware offset (us) | Observed RF offset range (us) | Correction (us) | DL gate half-width (us) |
|---|---:|---:|---:|---:|
| 50Hz | -9002 | -8612…-8550 | legacy wide gate | 750 |
| 100Hz Full | -2195 | -1910…-1897 | +295 | 250 |
| 150Hz | -595 | -324…-307 | +280 | 105 |
| 250Hz | -500 | -252…-234 | +260 | 80 |
| 333Hz Full | -429 | -221…-209 | +210 | 73 |
| 500Hz | -293 | -100…-73 | +200 | 31 |

Corrections are rounded centers supported by the observed ranges, not newly
claimed exact hardware constants. The 50Hz/250Hz classification models retain
their existing successful settings. All rate values are documented in code.

**500Hz safety issue:** its actual downlink starts inside the old ±100us uplink
acceptance interval. Simply widening the downlink gate would leave overlapping
direction definitions. The 500Hz uplink gate is now ±45us; the captured 2,128 real
RC packets stayed within approximately ±16us. Its downlink interval is
[-124,-62]us. The gates are disjoint, with a gap; on-grid RC at a telemetry nonce
remains RC, and a downlink never updates channels or the uplink anchor.
All other uplink tolerances remain ±100us. A calculated 10us minimum gap also
protects against future calibration edits accidentally overlapping the windows.
Packets in gaps are discarded, not guessed.

Calibration can require revision on other hardware/firmware or if conditions
exceed these observed bounds. The monitor must not fabricate telemetry to hide
that limitation. This is receive-only monitoring, not a flight-control receiver.
