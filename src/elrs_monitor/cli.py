"""Command-line entry point; text, JSON, and curses presentation modes."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

import numpy as np

from . import __version__
from .constants import DEFAULT_OFFSETS
from .decoder import validate_config
from .engine import run_receiver
from .profiles import canonical_rate, canonical_switch
from .protocol import parse_uid


def format_frame(frame):
    prefix = f"{frame['time_s']:10.6f}s {frame['channel_hz']/1e6:9.4f}MHz"
    if 'binding' in frame:
        b = frame['binding']
        state = 'DISCOVERED' if b['confirmed'] else 'candidate'
        return (f"{prefix} BIND {state} UID={b['uid']} "
                f"{b['confirmations']}/{b['required']} CRC=OK (four received bytes; zero-padded prefix)")
    if 'sync' in frame:
        s = frame['sync']
        return f"{prefix} SYNC nonce={s['nonce']:3d} hop_index={s['fhss_index']:3d} CRC=OK ID=OK"
    if 'rc' in frame:
        rc = frame['rc']
        updates = rc.get('channel_updates') or [dict(channel=i+1,value=v)
                                               for i,v in enumerate(rc['channels_raw10'])]
        values = ' '.join(f"CH{u['channel']}={u['value']:4d}" for u in updates)
        return f"{prefix} RC   {values} armed={int(rc['armed'])} CRC=OK"
    return f"{prefix} DATA {frame['packet_hex']} CRC=OK"


class TextObserver:
    def __init__(self, json_output=False):
        self.json_output = json_output
        self.last_status = 0

    def on_frame(self, frame):
        print(json.dumps(frame,separators=(',',':')) if self.json_output else format_frame(frame), flush=True)

    def on_message(self, message, level='info'):
        print(f'{level}: {message}',file=sys.stderr,flush=True)

    def on_status(self, status):
        if status['done']:
            print('summary: '+json.dumps(status,sort_keys=True),file=sys.stderr,flush=True)
        elif time.monotonic()-self.last_status >= 1:
            c = status['counters']
            gap = status.get('last_rc_gap_seconds')
            gaps = '--' if gap is None else f"{gap:.3f}/{status.get('max_rc_gap_seconds',gap):.3f}s"
            if status.get('capture_mode')=='hop-following' and getattr(self,'radio_diagnostics',False):
                print(f"radio: tunes={c.get('retunes',0)} max_control={status['retune_max_seconds']*1000:.3f}ms "
                      f"excluded={status.get('retune_discarded_seconds',0):.3f}s",file=sys.stderr,flush=True)
            print(f"status: phase={status.get('phase','monitoring')} sync={'locked' if status['locked'] else 'searching'} "
                  f"PHY={c.get('phy_candidates',0)} SYNC={c.get('sync',0)} RC={c.get('rc',0)} DATA={c.get('data',0)} "
                  f"reject(CRC/FEC/ID/mode)={c.get('crc_failures',0)}/{c.get('fec_failures',0)}/"
                  f"{c.get('identity_failures',0)}/{c.get('unsupported_sync',0)+c.get('mode_failures',0)} "
                  f"iq={status['source_seconds']:.1f}s behind={status.get('processing_lag_seconds',0):.3f}s "
                  f"RC_gap(last/max)={gaps} queue={status['queue_depth']}/6 "
                  f"app_dropped={status['application_dropped_seconds']:.3f}s",
                  file=sys.stderr,flush=True)
            self.last_status = time.monotonic()


def on_off(value):
    if isinstance(value,bool):
        return value
    if value.lower() in ('on','true','yes','1'):
        return True
    if value.lower() in ('off','false','no','0'):
        return False
    raise argparse.ArgumentTypeError('Use on or off')


def link_settings(args):
    """Build decoder settings exclusively from parsed command-line arguments."""
    settings = dict(model_match=args.model_match,model_id=args.model_id,
                    packet_rate=args.packet_rate,switch_mode=args.switch_mode,ota_version=4)
    if args.discover_binding:
        settings['discover_binding'] = True
    elif args.uid is not None:
        settings['uid'] = args.uid
    else:
        settings['binding_phrase'] = args.binding_phrase
    return settings


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description='Receive-only HackRF ELRS monitor: 2.4GHz LoRa, OTA-v4. No configuration files.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Compatible rate / switch-mode combinations:
  50, 150, 250, 500Hz    Hybrid or Wide
  100, 333Hz Full       8ch, 12ch, or 16ch (select explicitly)
Rates accept numbers or names such as '333Hz Full'. No FLRC/GFSK/D/F modes.
250Hz/Hybrid and 50Hz/Wide have recorded-RF validation; other PHY profiles are experimental.
Binding discovery also has a real RF capture fixture.

Examples:
  %(prog)s --bind-phrase 'YOUR PHRASE' --tui
  %(prog)s --bind-phrase 'YOUR PHRASE' --tui --model-match on --model-id 11
  %(prog)s --bind-phrase 'YOUR PHRASE' --rate 333 --switch-mode 16ch --tui
  %(prog)s --bind-phrase 'YOUR PHRASE' --replay capture.iq --pace --tui
  %(prog)s --uid 00:00:12:34:56:78 --rate 50 --tui
  %(prog)s --discover-binding --rate 50 --switch-mode Wide --model-match on --model-id 0 --tui

Choose exactly one identity: --bind-phrase, --uid, or --discover-binding.
UIDs accept 4 or 6 hex bytes: colon/hyphen separated or compact hex.
Four bytes are padded with 00:00; this does not recover the original MAC/phrase.
Discovery listens for binding at 50Hz / 2440.4MHz, regardless of the normal rate.
Ordinary traffic does NOT announce its binding UID. Start listening before BIND.
Three repeated valid binding messages learn a UID, then monitoring continues.
--rate / --switch-mode / --model-match / --model-id select NORMAL operation.
After binding ends, a matching CRC-valid normal sync is required before RC output.
Defaults remain 250Hz / Hybrid / Model Match off / ID 0; no automatic rate discovery.
There is no stop-after-discovery mode or implicit time limit. --seconds limits the
whole session; EOF/time limit before learning any UID is an error.
The learned UID stays in memory across IQ gaps; no settings are saved automatically.
Only one binding transmitter should be active. No receiver is required.

For normal monitoring, rate/mode/Model Match must match the transmitter.
Live: sync-channel acquisition then automatic receive-only FHSS following at 10Msps.
Retune/settling gaps can miss packets, especially at faster rates. No full-rate guarantee.
Loss of hop lock returns to sync acquisition; learned identity remains in memory.
Replay: legacy fixed-2440MHz raw IQ; it cannot reproduce live retuning timing.
Binding phrases are identity seeds, not secrets or encryption/authentication keys.
Startup shows the supplied phrase, full base UID, and Model Match-adjusted sync bytes.
The TUI keeps the base UID visible; startup/status text goes to stderr, not JSON stdout.
TUI keys: q/Esc quit; Space pause; r reset min/max; arrows/PageUp/PageDown scroll.""")
    ap.add_argument('--version',action='version',version=f'%(prog)s {__version__}')
    link = ap.add_argument_group('link settings')
    identity = link.add_mutually_exclusive_group(required=True)
    identity.add_argument('--bind-phrase','--binding-phrase',dest='binding_phrase',
                          metavar='PHRASE',help='Identity seed (not a secret); quote spaces; its derived base UID is shown at startup')
    identity.add_argument('--uid',type=parse_uid,metavar='HEX',
                          help='Known UID: 4 or 6 hexadecimal bytes, e.g. 12:34:56:78; mutually exclusive with phrase/discovery')
    identity.add_argument('--discover-binding',action='store_true',
                          help='Learn UID from 3 BIND messages, then monitor using normal link settings below (no TX)')
    match = link.add_mutually_exclusive_group()
    match.add_argument('--model-match',nargs='?',const=True,default=False,type=on_off,
                       metavar='on|off',help='Model Match (default: off; bare flag means on)')
    match.add_argument('--no-model-match',dest='model_match',action='store_false',
                       help='Alias for --model-match off')
    link.add_argument('--model-id',type=int,default=0,metavar='ID',
                      help='Model Match ID 0..63 (default: 0); ignored when off; does not enable matching')
    link.add_argument('--packet-rate','--refresh-rate','--rate',dest='packet_rate',type=canonical_rate,
                      default=None,metavar='HZ',
                      help='LoRa 50, 100, 150, 250, 333, 500Hz (default: 250); 100/333 use Full packets')
    link.add_argument('--switch-mode',type=canonical_switch,default=None,metavar='MODE',
                      help='Hybrid/Wide, or Full 8ch/12ch/16ch (default: Hybrid; combinations below)')
    display = ap.add_argument_group('display and logging')
    presentation = display.add_mutually_exclusive_group()
    presentation.add_argument('--tui',action='store_true',help='Interactive channel dashboard, minimum 80x22 (default: off)')
    presentation.add_argument('--format',choices=('human','json'),default='human',
                              help='Frame output on stdout without --tui (default: human); status on stderr')
    display.add_argument('--jsonl',type=Path,metavar='PATH',
                         help='Create a new owner-only JSONL frame log; never overwrite (default: no log)')
    display.add_argument('--radio-diagnostics',action='store_true',
                         help='Show additional live retune timing diagnostics (default: off)')
    display.add_argument('--stale-after',type=float,default=1.0,metavar='SECONDS',
                         help='Positive TUI stale-sample age threshold (default: 1 second)')
    capture = ap.add_argument_group('capture and replay')
    capture.add_argument('--replay',type=Path,metavar='PATH',
                         help='Read headerless signed-int8 interleaved IQ at 10Msps / 2440MHz (default: live HackRF)')
    capture.add_argument('--pace',action='store_true',
                         help='Pace --replay at original sample rate (default: replay as fast as possible)')
    capture.add_argument('--seconds',type=float,metavar='SECONDS',
                         help='Limit total session sample time, including discovery, minimum 0.001 (default: until stopped/EOF); TUI stays open')
    capture.add_argument('--workers',type=int,default=2,metavar='N',
                         help='Parallel DSP workers, 1..8 (default: 2)')
    capture.add_argument('--offset',type=int,nargs='+',default=None,metavar='HZ',
                         help='Replay only: fixed-center channel offsets (default: all nine); live follows FHSS automatically')
    radio = ap.add_argument_group('HackRF receive settings (live capture only)')
    radio.add_argument('--lna',type=int,choices=range(0,41,8),default=16,metavar='DB',
                       help='RX LNA gain, 0..40 dB in steps of 8 (default: 16)')
    radio.add_argument('--vga',type=int,choices=range(0,63,2),default=16,metavar='DB',
                       help='RX VGA gain, 0..62 dB in steps of 2 (default: 16); RF amp and antenna bias always off')
    radio.add_argument('--serial',metavar='SERIAL',help='Select HackRF serial number (default: first available device)')
    args = ap.parse_args(argv)
    if args.offset is not None and not args.replay:
        ap.error('--offset applies only to fixed-center --replay; live capture follows FHSS')
    if args.offset is None:
        args.offset = list(DEFAULT_OFFSETS)
    if args.discover_binding and 400000 not in args.offset:
        ap.error('--discover-binding requires the sync channel: include --offset 400000')
    args.packet_rate = args.packet_rate or '250Hz'
    args.switch_mode = args.switch_mode or 'Hybrid'
    if not 0 <= args.model_id <= 63:
        ap.error('--model-id must be in 0..63')
    if args.seconds is not None and (not np.isfinite(args.seconds) or args.seconds < .001):
        ap.error('--seconds must be finite and at least 0.001')
    if not np.isfinite(args.stale_after) or args.stale_after <= 0:
        ap.error('--stale-after must be finite and positive')
    if not 1 <= args.workers <= 8:
        ap.error('--workers must be in 1..8')
    if args.pace and not args.replay:
        ap.error('--pace requires --replay')
    if len(set(args.offset)) != len(args.offset) or any(o not in DEFAULT_OFFSETS for o in args.offset):
        ap.error('--offset must select unique values from -3600000 to 4400000 in 1000000Hz steps')
    try:
        validate_config(link_settings(args))
    except ValueError as exc:
        ap.error(str(exc))
    if args.tui and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        ap.error('--tui requires an interactive terminal on stdin and stdout')
    if args.tui and os.environ.get('TERM','dumb') in ('','dumb','unknown'):
        ap.error('--tui requires a usable TERM (for example xterm-256color)')
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        config = link_settings(args)
        if args.tui:
            from .tui import run_tui
            return run_tui(args,config)
        observer = TextObserver(args.format == 'json')
        observer.radio_diagnostics = args.radio_diagnostics
        run_receiver(args,config,observer)
        return 0
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f'error: {exc}',file=sys.stderr)
        return 1
