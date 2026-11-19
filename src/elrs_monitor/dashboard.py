"""Pure instrument-panel layout. No radio, timers, animation, or terminal IO."""
from dataclasses import dataclass
from itertools import groupby
import textwrap
import unicodedata


@dataclass(frozen=True)
class Line:
    text: str
    role: str = 'normal'
    spans: tuple = ()


def cells(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1
               for c in text)


def clip(text, width):
    result = [];used = 0
    for c in str(text):
        c = c if c.isprintable() else ' '
        size = cells(c)
        if used+size > max(0,width):
            break
        result.append(c);used += size
    return ''.join(result)


def line(spans):
    spans = tuple((str(text),role) for text,role in spans if text)
    return Line(''.join(t for t,r in spans),spans=spans)


def fit(spans, width):
    out = [];used = 0
    for text,role in spans:
        value = clip(text,width-used)
        out.append((value,role));used += cells(value)
        if used >= width:
            break
    if used < width:
        out.append((' '*(width-used),'normal'))
    return out


def framed(left, width, right=None, left_width=None):
    inside = width-2
    spans = [('│','border')]
    if right is None:
        spans += fit(left,inside)
    else:
        spans += fit(left,left_width)+[('│','border')]+fit(right,inside-left_width-1)
    return line(spans+[('│','border')])


def gauge(value, width, role='primary', center=False):
    width = max(3,width);n = width-2
    if value is None:
        return [('['+'·'*n+']','dim')]
    level = max(0,min(1,value))*n
    whole = int(level);fraction = int((level-whole)*8)
    body = [('█',role)]*whole
    if whole < n:
        if fraction:
            body.append(('▏▎▍▌▋▊▉'[fraction-1],role))
        body += [('░','track')]*(n-len(body))
    if center and n > 4:
        body[n//2] = ('│','dim')
    spans = [('[','dim')]
    for color,group in groupby(body,key=lambda pair:pair[1]):
        spans.append((''.join(c for c,r in group),color))
    return spans+[(']','dim')]


def phase_for(view):
    s = view['status']
    if view['error']:return 'ERROR','error'
    if view['finished'] or s['done']:return 'STOPPED / EOF','warning'
    if view['input_idle'] > 1:return 'IQ STALLED','warning'
    if view.get('discover_binding'):return 'LISTENING FOR BIND','warning'
    if s['locked']:
        return ('HOP FOLLOWING' if s.get('capture_mode')=='hop-following' else 'SYNC LOCKED'),'good'
    if s.get('phase')=='reacquiring':return 'REACQUIRING SYNC','warning'
    return ('WAITING NORMAL SYNC' if s.get('discovered_uid') else 'SEARCHING SYNC'),'warning'


def telemetry_display(view, paused=False):
    t = view.get('telemetry') or {};values = t.get('values')
    state = t.get('freshness','UNSEEN')
    if values and state=='fresh':
        state = 'LIVE' if (view['status']['locked'] and not view['status']['done']
                           and view['input_idle']<=1 and not paused) else 'HELD'
    role = 'good' if state=='LIVE' else 'dim' if not values else 'warning'
    return t,values,state,role


def sidebar(view, available, paused=False):
    s = view['status'];c = s['counters']
    gap = s.get('last_rc_gap_seconds')
    last = '--' if view['last_channel_hz'] is None else f"{view['last_channel_hz']/1e6:.4f} MHz"
    cfo = '--' if view['cfo_hz'] is None else f"{view['cfo_hz']:+.0f} Hz"
    behind = s.get('processing_lag_seconds',max(0,s['source_seconds']-s['processed_through_s']))
    rows = [[(' RECEIVER TELEMETRY','section')]]
    def metric(label,value,role='number'):
        rows.append([(f'  {label:<12} ','dim'),(str(value),role)])
    link = view.get('link') or {}
    metric('MODE / TLM',f"{link.get('link_mode') or '--'} / {link.get('telemetry_ratio') or '--'}")
    t,values,state,role = telemetry_display(view,paused)
    metric('RX STATS',state+('' if t.get('age') is None else f" {t['age']:.1f}s"),role)
    if values:
        rssi = lambda key:'--' if values[key] is None else str(values[key])
        metric('RSSI 1 / 2',f"{rssi('uplink_rssi_1_dbm')}/{rssi('uplink_rssi_2_dbm')} dBm",role)
        metric('LQ / SNR',f"{values['uplink_lq_percent']}% / {values['uplink_snr_db']:+.1f} dB",role)
        metric('ANT / MM',f"{values['active_antenna']} / {'yes' if values['model_match'] else 'no'}",role)
        metric('TRUE DIVERS.',str(values['true_diversity_available']),role)
    else:
        rows.append([('  No receiver stats received.','dim')])
    rows += [[],[(' MONITOR DIAGNOSTICS','section')]]
    metric('RF',last);metric('CFO',cfo)
    metric('OUTPUT',f"{view['output_rc_per_s']} RC/s",'primary')
    metric('GAP last/max','--' if gap is None else f"{gap*1000:.0f}/{s.get('max_rc_gap_seconds',gap)*1000:.0f} ms")
    metric('DSP behind',f'{behind*1000:.0f} ms','warning' if behind>.15 else 'good')
    metric('QUEUE',f"{s['queue_depth']} / {s['queue_capacity']}")
    metric('APP DROP',f"{s['application_dropped_seconds']*1000:.0f} ms",'warning' if s['application_dropped_seconds'] else 'good')
    metric('RETUNES',c.get('retunes',0))
    metric('EXCLUDED',f"{s.get('retune_discarded_seconds',0):.3f} s")
    metric('TLM early',c.get('telemetry_early_retunes',0))
    rows += [[],[(' EVENTS','section')]]
    # Wrap only genuine receiver events, never invent activity/signal quality.
    for level,message in reversed(view['events'][-3:]):
        color = 'error' if level=='error' else 'warning' if level=='warning' else 'dim'
        wrapped = textwrap.wrap(str(message),max(8,available-4))
        rows.extend([[('  '+text,color)] for text in wrapped[:2]])
    return rows


ASCII = str.maketrans({'╭':'+','╮':'+','╰':'+','╯':'+','─':'-','│':'|','●':'o','○':'o',
                      '·':'.','░':'.','█':'#','▏':'#','▎':'#','▍':'#','▌':'#','▋':'#','▊':'#','▉':'#',
                      '↑':'^','↓':'v','↕':'/','—':'-','µ':'u','…':'.'})


def render_lines(view,width,height,paused=False,scroll=0,unicode=True):
    if width < 80 or height < 22:
        return [Line('ELRS MONITOR','title'),
                Line('Terminal too small: need at least 80 columns x 22 rows.','warning'),
                Line('Acquisition continues. Resize terminal, or press q to quit.')][:max(0,height)]
    fw = width-1;inside = fw-2
    s = view['status'];counts = s['counters']
    phase,color = phase_for(view)
    if paused and not view['error'] and not s['done']:
        phase,color = 'DISPLAY PAUSED','warning'
    title = ' ELRS MONITOR '
    if paused:
        title += '| DISPLAY PAUSED '+('(capture stopped)' if s['done'] else '(acquisition continues)')+' '
    badge = ' RECEIVE ONLY '
    heading = [('╭─','border'),(title,'title')]
    rule = max(0,fw-3-cells(title)-cells(badge))
    heading += [('─'*rule,'border'),(badge,'badge_rx'),('╮','border')]
    match = 'ON ID '+str(view['model_id']) if view['model_match'] else 'OFF'
    setup = f" {view['source_label']}  ·  2.4GHz {view['packet_rate']} / {view['switch_mode']}  ·  Model Match {match}"
    identity = view.get('identity')
    identity_text = ' UID --' if identity is None else (
        f" UID {identity['uid']} | sync {identity['sync_uid']} | "
        +(f"phrase {identity['binding_phrase']!r}" if identity['source']=='phrase' else
          'learned UID (00:00 prefix)' if identity['source']=='binding' else 'explicit UID'))
    lines = [line(heading),framed([(setup,'dim')],fw),framed([(identity_text,'secondary')],fw)]

    if view.get('discover_binding'):
        binding = view.get('binding');uid = s.get('discovered_uid')
        confirmations = s.get('binding_confirmations',0)
        body = [
            [('  '+phase+'  ','badge_'+color)],[],
            [(' PASSIVE BIND ACQUISITION','section')],[],
            [('  Confirmations  ','dim')]+gauge(confirmations/3,min(30,inside-24),'secondary')+[(f'  {confirmations}/3','number')],
            [('  Confirmed UID: '+uid if uid else '  No UID confirmed yet.','good' if uid else 'dim')],
            [('  Last candidate: '+binding['uid'] if binding else '  No binding message received yet.','secondary')],[],
            [('  Put ONE transmitter into BIND mode after starting this listener.','title')],
            [('  Ordinary 50Hz traffic does NOT disclose the UID.','warning')],
            [('  Receive only. No receiver or transmit handshake required.','dim')],
            [('  Four UID bytes are received; prefix 00:00 is reconstructed.','dim')],[],
            [(f"  After discovery: {view['packet_rate']} / {view['switch_mode']} / MM {match}",'number')],
            [('  Three matching repeats learn UID; normal sync unlocks channels.','dim')],
            [(f"  PHY {counts.get('phy_candidates',0)}  BIND {counts.get('binding_packets',0)}  "
              f"CRC/FEC rejected {counts.get('crc_failures',0)+counts.get('fec_failures',0)}",'dim')]]
        for parts in body[:height-4]:lines.append(framed(parts,fw))
        while len(lines)<height-1:lines.append(framed([],fw))
    else:
        summary = [(' '+phase+' ','badge_'+color),('  ', 'normal'),
                   (f"RC {counts.get('rc',0)}",'primary'),
                   (f"  SYNC {counts.get('sync',0)}  DATA {counts.get('data',0)}",'number'),
                   (f"  C/F/I/M {counts.get('crc_failures',0)}/{counts.get('fec_failures',0)}/"
                    f"{counts.get('identity_failures',0)}/{counts.get('unsupported_sync',0)+counts.get('mode_failures',0)}",'dim')]
        lines.append(framed(summary,fw))
        side = 32 if fw>=115 else 0
        main = inside-side-1 if side else inside
        visible = min(len(view['channels']),max(1,height-11))
        scroll = max(0,min(scroll,len(view['channels'])-visible))
        behind = s.get('processing_lag_seconds',max(0,s['source_seconds']-s['processed_through_s']))
        arm = view['armed'];active = s['locked'] and not s['done'] and view['input_idle']<=1 and not paused
        armed = '--' if arm['value'] is None else (arm['description'] if active and arm['freshness']=='fresh' else
                f"{arm['freshness'] if arm['freshness']=='STALE' else 'HELD'}/last{arm['value']}")
        arm_role = ('dim' if arm['value'] is None else 'warning' if not active or arm['freshness']!='fresh'
                    else 'error' if arm['value'] else 'good')
        last = '--' if view['last_channel_hz'] is None else f"{view['last_channel_hz']/1e6:.4f} MHz"
        cfo = '--' if view['cfo_hz'] is None else f"{view['cfo_hz']:+.0f} Hz"
        body = [
            [(f" IQ {s['source_seconds']:.2f}s  ",'dim'),(f"DSP behind {behind:.3f}s",'warning' if behind>.15 else 'good'),
             (f"  queue {s['queue_depth']}/{s['queue_capacity']}  app drops {s['application_dropped_seconds']:.3f}s",'warning' if s['application_dropped_seconds'] else 'dim')],
            [(f' RF {last}  CFO {cfo}  ','dim'),(f"RC/s {view['output_rc_per_s']}",'primary'),('  Arm ','dim'),(armed,arm_role)],
            [(f" CH  {'Function':<12} {'Raw/max':>9} "
              +f"{'LEVEL':<{max(5,main-57)}}"
              +f" {'MIN..MAX':>11} {'AGE':>6} {'STATE':<8} ",'panel')]]
        for c in view['channels'][scroll:scroll+visible]:
            state = c['freshness']
            if state=='fresh' and not active:state='HELD'
            row_role = 'dim' if state=='UNSEEN' else 'warning' if state in ('STALE','HELD') else 'number'
            bar_role = 'primary' if c['number']<=4 else 'secondary'
            if state!='fresh':bar_role=row_role
            value = '--' if c['value'] is None else str(c['value'])
            if c['kind']=='armed' and c['value'] is not None and state=='fresh':
                state=c['description'];bar_role='error' if c['value'] else 'good'
            elif c['kind']=='switch' and c['bits']==3 and c['value'] is not None and c['value']>=6:value+='M'
            limits = '--..--' if c['minimum'] is None else f"{c['minimum']}..{c['maximum']}"
            age = '--' if c['age'] is None else f"{c['age']:.1f}s" if c['age']<100 else '>99s'
            state_text = 'LIVE' if state=='fresh' else state
            prefix = f" {c['number']:02d}  {c['label']:<12} {value:>4}/{(1<<c['bits'])-1:<4} "
            suffix = f" {limits:>11} {age:>6} {state_text:<8} "
            gauge_width = max(5,main-cells(prefix)-cells(suffix))
            body.append([(prefix,row_role)]+gauge(c['normalized'],gauge_width,bar_role,c['kind']=='analog')+
                        [(f" {limits:>11} ",'dim'),
                         (f"{age:>6} ",'good' if c['freshness']=='fresh' and active else row_role),
                         (f"{state_text:<8} ",'good' if state=='fresh' else bar_role)])
        gap = s.get('last_rc_gap_seconds')
        gaps = '--' if gap is None else f"{gap:.3f}/{s.get('max_rc_gap_seconds',gap):.3f}s"
        t,values,tlm_state,tlm_role = telemetry_display(view,paused)
        link = view.get('link') or {}
        telemetry_text = f" {link.get('link_mode') or '--'} TLM {link.get('telemetry_ratio') or '--'} | RX {tlm_state}"
        if values:
            rssi = values['uplink_rssi_1_dbm']
            telemetry_text += (f" RSSI {rssi if rssi is not None else '--'}dBm"
                               f" LQ {values['uplink_lq_percent']}% SNR {values['uplink_snr_db']:+.1f}dB"
                               f" age {t['age']:.1f}s")
        body.append([(telemetry_text,tlm_role)])
        body.append([(f" CH {scroll+1}-{scroll+visible}/{len(view['channels'])} | decoded RC gap last/max {gaps}",'dim')])
        if s.get('capture_mode')=='hop-following':
            footer = f" FHSS 80 channels | retunes {counts.get('retunes',0)} | excluded {s.get('retune_discarded_seconds',0):.3f}s"
            footer += (f" | ctrl max {s.get('retune_max_seconds',0)*1000:.2f}ms" if s.get('radio_diagnostics') else
                       f" | T/H {counts.get('off_grid',0)}/{counts.get('hop_frequency_failures',0)}")
        else:footer=f" PARTIAL BAND: {view['channel_count']} hop channels. Held values are not live control."
        body.append([(footer,'dim')])
        slots = height-1-len(lines)-len(body)
        if side:
            if slots>0:body.append([(' Raw OTA units, not µs. │ marks half scale.','dim')]);slots-=1
        else:
            for level,message in view['events'][-slots:] if slots>0 else []:
                body.append([(' '+message,'error' if level=='error' else 'warning' if level=='warning' else 'dim')]);slots-=1
        body += [[] for _ in range(max(0,slots))]
        right = sidebar(view,side,paused) if side else []
        for i,parts in enumerate(body):
            lines.append(framed(parts,fw,right[i] if i<len(right) else [] if side else None,main))
    keys = ' q/Esc quit · Space pause · r ranges · ↑↓/Pg scroll '
    bottom = [('╰─','border'),(keys,'hotkey'),('─'*max(0,fw-3-cells(keys)),'border'),('╯','border')]
    lines.append(line(bottom))
    result = lines[:height]
    if not unicode:
        result = [Line(l.text.translate(ASCII),l.role,tuple((t.translate(ASCII),r) for t,r in l.spans)) for l in result]
    return result
