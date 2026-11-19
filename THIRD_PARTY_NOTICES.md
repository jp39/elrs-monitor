# Copyright, provenance and third-party notices

ELRS Monitor's original contributions are copyright (C) 2026 ELRS Monitor
contributors, licensed under **GNU GPL version 3 only** (`GPL-3.0-only`). See
[LICENSE](LICENSE) for the complete terms. Upstream authors retain copyright in
any adapted material. This project does not claim exclusive authorship of the
upstream implementations or relicense their material under MIT.

## ExpressLRS

- Project: https://github.com/ExpressLRS/ExpressLRS
- Reference: ExpressLRS 4.1.0, commit
  `a9d4a9cb5b5687c4c9d7e9e7fbdf44ad93651da6`.
- [Upstream license](https://github.com/ExpressLRS/ExpressLRS/blob/a9d4a9cb5b5687c4c9d7e9e7fbdf44ad93651da6/LICENSE):
  GNU GPL version 3. Copyright remains with the respective ExpressLRS contributors.
- **Adapted implementation:** `src/elrs_monitor/fhss.py` translates the UID seed,
  pseudo-random generator and FHSS sequence-building logic from
  `src/lib/OTA/OTA.cpp` (`OtaGetUidSeed`), `src/lib/FHSS/random.cpp`, and
  `src/lib/FHSS/FHSS.cpp` (`FHSSrandomiseFHSSsequenceBuild`). The Python adaptation
  specializes the sequence to ISM2G4 and adds independent hop-clock prediction.
  These adaptations were made as part of this monitor's 2026 development; the
  upstream firmware files are not included as separate runtime dependencies.
- Other consulted material includes OTA packet layouts, CRC, UID derivation,
  Model Match, Hybrid/Wide and Full channel semantics, binding packets,
  rate/airtime tables, receiver telemetry and firmware scheduling. Relevant
  implementations and constants are in `protocol.py`, `profiles.py`,
  `binding.py`, and `telemetry.py`; timing provenance is documented in
  `docs/telemetry-timing.md`.
- Deterministic test fixtures include sequence output and packet/CRC results
  checked against the reference firmware. No firmware executable is bundled.

## gr-lora_sdr

- Project: https://github.com/tapparelj/gr-lora_sdr
- Reference commit: `862746dd1cf635c9c8a4bfbaa2c3a0ec3a5306c9`.
- [Upstream license](https://github.com/tapparelj/gr-lora_sdr/blob/862746dd1cf635c9c8a4bfbaa2c3a0ec3a5306c9/LICENSE):
  GNU GPL version 3. Copyright remains with its respective contributors.
- Consulted for LoRa Hamming coding, deinterleaving and whitening conventions,
  particularly `lib/hamming_enc_impl.cc`, `lib/deinterleaver_impl.cc`,
  `lib/dewhitening_impl.cc`, and `lib/tables.h`. The monitor implements coding,
  whitening and ELRS long-interleaver handling in Python in `protocol.py`.
  This notice acknowledges that provenance and does not imply that the original
  GNU Radio implementation is bundled or required at runtime.

## External runtime libraries and drivers

These are separately installed dependencies, **not vendored into this repository
or its Python wheel**. They retain their own licenses; the project's GPL does not
relicense them. Redistributing them requires retaining their applicable notices
and satisfying their licenses, in addition to this project's obligations.

| Component | License | Project |
|---|---|---|
| NumPy | BSD-3-Clause | https://github.com/numpy/numpy |
| SciPy | BSD-3-Clause, with additional notices for bundled components | https://github.com/scipy/scipy |
| libhackrf host library | BSD-3-Clause | https://github.com/greatscottgadgets/hackrf/tree/main/host/libhackrf |
| SoapySDR core (optional) | Boost Software License 1.0 | https://github.com/pothosware/SoapySDR |
| SoapyHackRF plugin (optional) | MIT | https://github.com/pothosware/SoapyHackRF |

Other SoapySDR plugins can have different licenses and are not covered by this
list. SoapyHackRF build provenance for the tested setup is in `docs/soapy.md`.
The libhackrf library license is distinct from licenses of other HackRF tools
or firmware; the latter are not shipped by this Python package.

### libhackrf C API notice

The native ctypes interface follows libhackrf's public C API. The following
notice is retained from its header:

> Copyright (c) 2012-2022 Great Scott Gadgets <info@greatscottgadgets.com>
> Copyright (c) 2012, Jared Boone <jared@sharebrained.com>
> Copyright (c) 2013, Benjamin Vernoux <titanmkd@gmail.com>
>
> All rights reserved.
>
> Redistribution and use in source and binary forms, with or without modification,
> are permitted provided that the following conditions are met:
>
> Redistributions of source code must retain the above copyright notice, this
> list of conditions and the following disclaimer.
>
> Redistributions in binary form must reproduce the above copyright notice,
> this list of conditions and the following disclaimer in the documentation
> and/or other materials provided with the distribution.
>
> Neither the name of Great Scott Gadgets nor the names of its contributors may
> be used to endorse or promote products derived from this software without
> specific prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
> AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
> IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
> ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
> LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
> DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
> SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
> CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
> OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
> OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## Specification references and project identity

Semtech SX1280/SX1281 datasheet Rev 3.2 was consulted for modem timing and packet
geometry. Semtech documentation, third-party logos and trademarks are not
relicensed by this project; no datasheet PDF is bundled.

ELRS Monitor is an independent project, not an official ExpressLRS, Semtech,
HackRF, GNU Radio or SoapySDR product. References and acknowledgements do not
imply endorsement.
