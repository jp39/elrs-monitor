"""Stdlib curses frontend. All terminal calls stay on the main thread."""
from dataclasses import dataclass
import threading

from .engine import run_receiver
from .decoder import identity_details
from .model import MonitorState


@dataclass(frozen=True)
class Line:
    text: str
    role: str = 'normal'


def bar(fraction, width=16):
    if fraction is None:
        return '['+'?'*width+']'
    filled = round(max(0, min(1, fraction))*width)
    return '['+'#'*filled+'-'*(width-filled)+']'


def render_lines(view,width,height,paused=False,scroll=0):
    """Pure renderer for deterministic tests and safe terminal resizing."""
    if width < 80 or height < 22:
        return [Line('ELRS MONITOR', 'title'),
                Line('Terminal too small: need at least 80 columns x 22 rows.', 'warning'),
                Line('Acquisition continues. Resize terminal, or press q to quit.')][:height]
    status = view['status']
    counts = status['counters']
    behind = status.get('processing_lag_seconds',max(0,status['source_seconds']-status['processed_through_s']))
    gap = status.get('last_rc_gap_seconds')
    gaps = '--' if gap is None else f"{gap:.3f}/{status.get('max_rc_gap_seconds',gap):.3f}s"
    if view['error']:
        phase, role = 'ERROR', 'error'
    elif view['finished'] or status['done']:
        phase, role = 'STOPPED / EOF', 'warning'
    elif view['input_idle'] > 1.0:
        phase, role = 'IQ STALLED', 'warning'
    else:
        if view.get('discover_binding'):
            phase, role = 'LISTENING FOR BIND', 'warning'
        else:
            waiting = 'WAITING NORMAL SYNC' if status.get('discovered_uid') else 'SEARCHING SYNC'
            if status.get('capture_mode')=='hop-following':
                phase,role = ('HOP FOLLOWING','good') if status['locked'] else (
                    'REACQUIRING SYNC' if status.get('phase')=='reacquiring' else waiting,'warning')
            else:
                phase, role = ('SYNC LOCKED', 'good') if status['locked'] else (waiting, 'warning')
    last = '--' if view['last_channel_hz'] is None else f"{view['last_channel_hz']/1e6:.4f} MHz"
    cfo = '--' if view['cfo_hz'] is None else f"{view['cfo_hz']:+.0f} Hz"
    title = 'ELRS MONITOR | RECEIVE ONLY'
    if paused:
        title += ' | DISPLAY PAUSED'
        title += ' (capture stopped)' if status['done'] else ' (acquisition continues)'
    if view.get('discover_binding'):
        binding = view.get('binding')
        uid = status.get('discovered_uid')
        lines = [Line(title,'title'),
                 Line(f"{view['source_label']} | BIND DISCOVERY | 50Hz LoRa / 2440.4MHz"),
                 Line(phase,role),
                 Line(f"IQ {status['source_seconds']:.2f}s | DSP behind {behind:.3f}s"
                      f" | app drops {status['application_dropped_seconds']:.3f}s"),
                 Line(f"PHY {counts.get('phy_candidates',0)} | BIND {counts.get('binding_packets',0)}"
                      f" | CRC/FEC rejected {counts.get('crc_failures',0)+counts.get('fec_failures',0)}"),
                 Line(f"RF {last} CFO {cfo}"),Line(''),
                 Line('Confirmed UID: '+uid if uid else 'No UID confirmed yet.','good' if uid else 'warning'),
                 Line(f"Best current repetition count: {status.get('binding_confirmations',0)}/3"),
                 Line('Last candidate: '+binding['uid'] if binding else 'No binding message received yet.'),
                 Line('Put ONE transmitter into BIND mode after starting this listener.','title'),
                 Line('Selecting ordinary 50Hz operation alone does NOT disclose the UID.','warning'),
                 Line('Discovery receives only; no actual receiver or transmit handshake required.'),
                 Line('Only four UID bytes are sent; 00:00 prefix is reconstructed, not the MAC.'),
                 Line(f"After discovery: {view['packet_rate']} / {view['switch_mode']} / "
                      +('MM ID '+str(view['model_id']) if view['model_match'] else 'MM off')),
                 Line('After three repeats, monitoring continues; normal sync unlocks channels.')]
        available = max(0,height-len(lines)-1)
        for level,message in view['events'][-available:] if available else []:
            lines.append(Line(message,'error' if level == 'error' else 'dim'))
        while len(lines) < height-1:
            lines.append(Line(''))
        lines.append(Line('q/Esc quit | Space pause display | capture continues after UID discovery','title'))
        return lines[:height]
    arm = view['armed']
    active = status['locked'] and not status['done'] and view['input_idle'] <= 1
    arm_text = '--' if arm['value'] is None else (
        arm['description'] if active and arm['freshness'] == 'fresh' else
        f"{arm['freshness'] if arm['freshness'] == 'STALE' else 'HELD'}/last{arm['value']}")
    visible = min(len(view['channels']),max(1,height-11))
    identity = view.get('identity')
    identity_text = 'UID --' if identity is None else (
        f"UID {identity['uid']} | sync {identity['sync_uid']} | "
        +(f"phrase {identity['binding_phrase']!r}" if identity['source']=='phrase' else
          ('learned UID (00:00 prefix)' if identity['source']=='binding' else 'explicit UID')))
    scroll = max(0,min(scroll,len(view['channels'])-visible))
    lines = [
        Line(title,'title'),
        Line(f"{view['source_label']} | 2.4GHz {view['packet_rate']} {view['switch_mode']} | "
             f"Model Match {'ON ID '+str(view['model_id']) if view['model_match'] else 'OFF'}"),
        Line(identity_text),
        Line(f"{phase} | SYNC {counts.get('sync',0)}  RC {counts.get('rc',0)}  DATA {counts.get('data',0)}"
             f" | reject CRC/FEC/ID/mode {counts.get('crc_failures',0)}/{counts.get('fec_failures',0)}/"
             f"{counts.get('identity_failures',0)}/{counts.get('unsupported_sync',0)+counts.get('mode_failures',0)}",role),
        Line(f"IQ {status['source_seconds']:.2f}s | DSP behind {behind:.3f}s"
             f" | queue {status['queue_depth']}/{status['queue_capacity']}"
             f" | app drops {status['application_dropped_seconds']:.3f}s",
             'warning' if status['application_dropped_seconds'] else 'normal'),
        Line(f"RF {last} CFO {cfo} | RC/s {view['output_rc_per_s']} | Arm {arm_text}"),
        Line(' CH   Function      Raw/max   Level                Seen min..max    Age State', 'title'),
    ]
    for c in view['channels'][scroll:scroll+visible]:
        value = '--' if c['value'] is None else str(c['value'])
        extent = (1 << c['bits'])-1
        limits = '--..--' if c['minimum'] is None else f"{c['minimum']}..{c['maximum']}"
        age = '--' if c['age'] is None else (f"{c['age']:.1f}s" if c['age'] < 100 else '>99s')
        state = c['freshness']
        if state == 'fresh' and (not status['locked'] or status['done'] or view['input_idle'] > 1):
            state = 'HELD'
        role = 'dim' if state == 'UNSEEN' else ('warning' if state in ('STALE','HELD') else 'normal')
        if c['kind'] == 'armed' and c['value'] is not None:
            fresh = state == 'fresh'
            state = c['description'] if fresh else state
            if c['value'] and fresh:
                role = 'error'
        elif c['kind'] == 'switch' and c['bits'] == 3 and c['value'] is not None and c['value'] >= 6:
            value += 'M'
        lines.append(Line(f" {c['number']:02d}   {c['label']:<12} {value:>4}/{extent:<4} "
                          f"{bar(c['normalized'])} {limits:>12} {age:>6} {state}", role))
    lines += [Line(f"CH {scroll+1}-{scroll+visible}/{len(view['channels'])} | decoded RC gap last/max {gaps} "
                       f"| raw OTA; STALE>{view['stale_after']:.1f}s",'dim'),
              Line((f"FHSS 80 channels | retunes {counts.get('retunes',0)} | excluded "
                    f"{status.get('retune_discarded_seconds',0):.3f}s"
                    +(f" | ctrl max {status.get('retune_max_seconds',0)*1000:.2f}ms"
                      if status.get('radio_diagnostics') else ' | gaps possible')
                    if status.get('capture_mode')=='hop-following' else
                    f"PARTIAL BAND: {view['channel_count']} hop channels. Last seen values are NOT failsafe/live control."), 'warning')]
    available = max(0, height-len(lines)-1)
    for level, message in view['events'][-available:] if available else []:
        lines.append(Line(message, 'error' if level == 'error' else ('warning' if level == 'warning' else 'dim')))
    while len(lines) < height-1:
        lines.append(Line(''))
    lines.append(Line('q/Esc quit | Up/Down/Pg scroll | Space pause | r reset min/max','title'))
    return lines[:height]


def draw_screen(screen, lines, attributes):
    import curses
    screen.erase()
    height, width = screen.getmaxyx()
    for row, line in enumerate(lines[:height]):
        # Leave the terminal's lower-right cell unused to avoid ERR on wrap.
        text = line.text[:max(0,width-1)].replace('\t',' ')
        if text:
            try:
                screen.addnstr(row, 0, text, max(0,width-1), attributes.get(line.role,0))
            except curses.error:
                # A resize may occur between getmaxyx and addnstr.
                pass
    screen.refresh()


def run_tui(args, config):
    # Lazy import lets headless operation work even without curses installed.
    import curses
    stop = threading.Event()
    state = MonitorState(source_label=('REPLAY '+args.replay.name) if args.replay else 'LIVE HackRF',
                         model_id=config['model_id'], model_match=config['model_match'],
                         channel_count=len(args.offset),stale_after=args.stale_after,
                         packet_rate=config['packet_rate'],switch_mode=config['switch_mode'],
                         discover_binding=config.get('discover_binding',False),identity=identity_details(config))

    def worker():
        error = None
        try:
            run_receiver(args, config, state, stop)
        except Exception as exc:
            error = str(exc)
        finally:
            state.complete(error)

    state.status['capture_mode'] = 'fixed-replay' if args.replay else 'hop-following'
    thread = threading.Thread(target=worker, name='elrs-receiver')

    def session(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(100)
        attributes = dict(title=curses.A_BOLD, dim=curses.A_DIM,
                          warning=curses.A_BOLD, error=curses.A_BOLD, good=curses.A_BOLD)
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                for index, color in enumerate((curses.COLOR_GREEN,curses.COLOR_YELLOW,curses.COLOR_RED),1):
                    curses.init_pair(index,color,-1)
                attributes.update(good=curses.color_pair(1)|curses.A_BOLD,
                                  warning=curses.color_pair(2), error=curses.color_pair(3)|curses.A_BOLD)
            except curses.error:
                pass
        thread.start()
        paused = False
        frozen = None
        scroll = 0
        while True:
            latest = state.snapshot()
            # EOF/errors must become visible even if display was paused earlier.
            if paused and ((latest['finished'] and not frozen['finished'])
                           or (latest['error'] and not frozen['error'])):
                paused = False
                frozen = None
            view = frozen if paused else latest
            height, width = screen.getmaxyx()
            scroll = max(0,min(scroll,max(0,len(view['channels'])-max(1,height-11))))
            draw_screen(screen,render_lines(view,width,height,paused,scroll),attributes)
            key = screen.getch()
            if key in (ord('q'),ord('Q'),27):
                break
            if key == curses.KEY_DOWN:
                scroll += 1
            elif key == curses.KEY_UP:
                scroll -= 1
            elif key == curses.KEY_NPAGE:
                scroll += max(1,height-11)
            elif key == curses.KEY_PPAGE:
                scroll -= max(1,height-11)
            elif key == curses.KEY_HOME:
                scroll = 0
            elif key == curses.KEY_END:
                scroll = len(view['channels'])
            if key == ord(' '):
                paused = not paused
                frozen = state.snapshot() if paused else None
            if key in (ord('r'),ord('R')):
                state.reset_ranges()
                if paused:
                    frozen = state.snapshot()
            # EOF/error intentionally leaves the final screen visible until q.

    try:
        curses.wrapper(session)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join()
    final = state.snapshot()
    error = final['error']
    if error:
        # Terminal is restored before a plain error reaches the caller.
        raise RuntimeError(error)
    uid = final['status'].get('discovered_uid')
    if uid:
        print(f'Discovered UID: {uid} (four received bytes; zero-padded prefix)')
    return 0
