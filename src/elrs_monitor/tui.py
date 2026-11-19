"""Stdlib curses frontend. All terminal calls stay on the main thread."""
import locale
import threading

from .dashboard import Line, render_lines, clip, cells

from .engine import run_receiver
from .decoder import identity_details
from .model import MonitorState


def bar(fraction, width=16):
    if fraction is None:
        return '['+'?'*width+']'
    filled = round(max(0, min(1, fraction))*width)
    return '['+'#'*filled+'-'*(width-filled)+']'


def configure_colors(curses):
    """Cyan/violet palette, with eight-color and monochrome fallbacks."""
    styles = dict(title=curses.A_BOLD, section=curses.A_BOLD,
                  dim=curses.A_DIM, border=curses.A_DIM, track=curses.A_DIM,
                  primary=curses.A_BOLD, secondary=0, number=curses.A_BOLD,
                  good=curses.A_BOLD, warning=curses.A_BOLD, error=curses.A_BOLD,
                  hotkey=curses.A_BOLD, panel=curses.A_BOLD,
                  badge_good=curses.A_REVERSE|curses.A_BOLD,
                  badge_warning=curses.A_REVERSE|curses.A_BOLD,
                  badge_error=curses.A_REVERSE|curses.A_BOLD,
                  badge_rx=curses.A_REVERSE|curses.A_BOLD)
    if not curses.has_colors():
        return styles
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    background = 233 if curses.COLORS >= 256 else curses.COLOR_BLACK
    if curses.COLORS >= 256:
        palette = dict(normal=(252,background),title=(255,background),border=(60,background),
                       dim=(244,background),track=(238,background),primary=(81,background),
                       secondary=(141,background),number=(255,background),good=(84,background),
                       warning=(221,background),error=(203,background),section=(81,background),
                       hotkey=(81,background),panel=(252,235),badge_good=(16,84),
                       badge_warning=(16,221),badge_error=(16,203),badge_rx=(231,61))
    else:
        palette = {name:(foreground,background) for name,foreground in dict(
            normal=curses.COLOR_WHITE,title=curses.COLOR_WHITE,border=curses.COLOR_CYAN,
            dim=curses.COLOR_WHITE,track=curses.COLOR_WHITE,primary=curses.COLOR_CYAN,
            secondary=curses.COLOR_MAGENTA,number=curses.COLOR_WHITE,good=curses.COLOR_GREEN,
            warning=curses.COLOR_YELLOW,error=curses.COLOR_RED,section=curses.COLOR_CYAN,
            hotkey=curses.COLOR_CYAN,panel=curses.COLOR_WHITE).items()}
        palette.update(badge_good=(curses.COLOR_BLACK,curses.COLOR_GREEN),
                       badge_warning=(curses.COLOR_BLACK,curses.COLOR_YELLOW),
                       badge_error=(curses.COLOR_WHITE,curses.COLOR_RED),
                       badge_rx=(curses.COLOR_WHITE,curses.COLOR_BLUE))
    for index,(name,(foreground,bg)) in enumerate(palette.items(),1):
        if index >= curses.COLOR_PAIRS:
            break
        try:
            curses.init_pair(index,foreground,bg)
            # Explicit track colors are already subdued in the 256-color theme.
            style = 0 if curses.COLORS >= 256 and name in ('border','track','dim') else styles.get(name,0)
            styles[name] = curses.color_pair(index)|style
        except curses.error:
            pass
    return styles


def draw_screen(screen, lines, attributes):
    import curses
    screen.erase()
    height,width = screen.getmaxyx()
    for row,item in enumerate(lines[:height]):
        column = 0
        for text,role in item.spans or ((item.text,item.role),):
            text = clip(text,max(0,width-1-column))
            if not text:
                continue
            try:
                screen.addstr(row,column,text,attributes.get(role,0))
            except curses.error:
                # Resize can race drawing; leave the lower-right cell unused.
                pass
            column += cells(text)
    screen.refresh()


def run_tui(args, config):
    # Lazy import lets headless operation work even without curses installed.
    import curses
    stop = threading.Event()
    state = MonitorState(source_label=('REPLAY '+args.replay.name) if args.replay else
                         'LIVE SoapySDR' if args.backend=='soapy' else 'LIVE HackRF',
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
        attributes = configure_colors(curses)
        screen.bkgd(' ',attributes.get('normal',0))
        unicode = 'utf' in (getattr(screen,'encoding',None) or locale.getpreferredencoding(False)).lower()
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
            draw_screen(screen,render_lines(view,width,height,paused,scroll,unicode=unicode),attributes)
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
