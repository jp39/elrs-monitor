"""Focused downlink direction/state checks; synthetic, not RF validation."""
import unittest

from elrs_monitor.follow import TrackingDecoder, HopFollower
from elrs_monitor.fhss import HopClock
from elrs_monitor.model import MonitorState
from elrs_monitor.dashboard import render_lines, cells
from elrs_monitor.profiles import PROFILES
from elrs_monitor.telemetry import downlink_offset
from test_modes import config, sync_packet, seal, observation
from test_hopping import FakeRadio


def synced(profile, code=7, mode=0, nonce=255):
    cfg = config(profile,'8ch' if profile.full_resolution else 'Wide')
    d = TrackingDecoder(cfg)
    packet = bytearray(sync_packet(profile,cfg,nonce))
    packet[4] |= code<<1 | mode<<5
    d.feed(observation(seal(packet,cfg),1.,profile))
    return d,cfg


def downlink(d,cfg,nonce=0,kind=0):
    p = d.profile
    raw = bytearray(p.packet_bytes)
    raw[0] = (3<<3 | 4 | kind) if p.full_resolution else kind
    start = 1 if p.full_resolution else 2
    if not p.full_resolution:raw[1]=3|128
    raw[start:start+4] = bytes([65|128,70|128,99|128,246])
    clock = HopClock.from_sync(d.anchor,p)
    slots = (nonce-clock.nonce)&255
    grid = clock.time_s+slots*p.interval
    b = observation(seal(raw,cfg,nonce),grid+downlink_offset(p),p)
    b['channel_hz'] = clock.frequency_at(grid,d.channels)
    return b


class TelemetryTests(unittest.TestCase):
    def test_physical_50hz_receiver_turnaround_delay(self):
        # Real connected TX+RX capture, not a packet sealed by the test helper.
        p = PROFILES['50Hz']
        cfg = dict(binding_phrase='julian',model_id=0,model_match=True,
                   packet_rate=p.name,switch_mode='Wide',ota_version=4)
        d = TrackingDecoder(cfg)
        sync = observation(bytes.fromhex('72006115085fec9e'),2.200251819711539,p)
        self.assertEqual(len(d.feed(sync)),1)
        anchor = d.anchor
        b = observation(bytes.fromhex('bc003780642f0007'),2.49163329375,p)
        b['channel_hz'] = 2425400000
        frame, = d.feed(b)
        self.assertGreater(frame['slot_timing_error_us'],250)
        self.assertLess(frame['slot_timing_error_us'],750)
        self.assertEqual(frame['inferred_nonce'],112)
        self.assertEqual(frame['telemetry']['uplink_rssi_1_dbm'],-55)
        self.assertIsNone(frame['telemetry']['uplink_rssi_2_dbm'])
        self.assertEqual(frame['telemetry']['uplink_snr_db'],11.75)
        self.assertIs(d.anchor,anchor)
        self.assertEqual(d.counts['rc'],0)
        self.assertEqual(d.feed(dict(b,time_s=b['time_s']+.001)),[])

    def test_all_profiles_directions_nonce_wrap_modes_and_no_clock_or_rc_mutation(self):
        for p in PROFILES.values():
            for mode in (0,1):
                with self.subTest(profile=p.name,mode=mode):
                    d,cfg = synced(p,mode=mode)
                    anchor = d.anchor
                    frame, = d.feed(downlink(d,cfg))
                    t = frame['telemetry']
                    self.assertEqual(frame['inferred_nonce'],0)
                    self.assertEqual(frame['direction'],'downlink')
                    self.assertEqual((t['uplink_rssi_1_dbm'],t['uplink_rssi_2_dbm']),(-65,-70))
                    self.assertEqual((t['uplink_snr_db'],t['uplink_lq_percent']),(-2.5,99))
                    self.assertEqual((t['active_antenna'],t['package_index'],t['stubborn_ack']),(1,3,True))
                    self.assertTrue(t['model_match'] and t['true_diversity_available'])
                    self.assertIs(d.anchor,anchor)
                    self.assertEqual(d.counts['rc'],0)
                    self.assertNotIn('rc',frame)
                    state = MonitorState(source_label='synthetic',model_id=11,channel_count=80,
                                         packet_rate=p.name,switch_mode=cfg['switch_mode'],clock=lambda:0)
                    state.on_frame(frame)
                    self.assertTrue(all(c.value is None for c in state.channels))
                    self.assertIsNone(state.armed.value)
                    self.assertEqual(d.link_status['link_mode'],'MAVLink' if mode else 'normal')
                    raw = downlink(d,cfg,kind=1)
                    self.assertEqual(d.feed(raw)[0]['telemetry']['kind'],'data_fragment')

    def test_off_wrong_slot_crc_frequency_and_ratio_changes_fail_closed(self):
        p = PROFILES['50Hz']
        d,cfg = synced(p)
        b = downlink(d,cfg)
        bad = dict(b,channel_hz=b['channel_hz']+1000000)
        self.assertEqual(d.feed(bad),[])
        self.assertEqual(d.feed(downlink(d,cfg,nonce=1)),[])
        raw = bytearray.fromhex(d.feed(b)[0]['packet_hex']);raw[3]^=1
        bad = observation(bytes(raw),b['time_s'],p);bad['channel_hz']=b['channel_hz']
        self.assertEqual(d.feed(bad),[])
        off,cfg = synced(p,code=0)
        self.assertEqual(off.feed(downlink(off,cfg)),[])
        self.assertEqual(off.link_status['telemetry_ratio'],'off')
        # A fresh valid sync turns telemetry off and switches payload mode.
        packet=bytearray(sync_packet(p,cfg,255));packet[4]|=1<<5
        d.feed(observation(seal(packet,cfg),1.,p))
        self.assertEqual(d.link_status['link_mode'],'MAVLink')
        self.assertEqual(d.feed(b),[])
        d.reset();self.assertIsNone(d.link_status)
        self.assertEqual(d.feed(b),[])

    def test_early_retune_and_independent_fresh_stale_held_dashboard(self):
        p = PROFILES['50Hz'];d,cfg = synced(p)
        s = FakeRadio();f = HopFollower(s,p,d.channels);f.track(d.anchor)
        s.now=1.006;f.step()
        self.assertEqual(f.counts['telemetry_early_retunes'],1)
        b = downlink(d,cfg);frame, = d.feed(b)
        wall=[0.]
        state=MonitorState(source_label='synthetic',model_id=11,channel_count=80,
                           clock=lambda:wall[0],packet_rate=p.name,switch_mode='Wide')
        state.on_frame(d.anchor);state.on_frame(frame)
        status=dict(state.status,locked=True,source_seconds=frame['time_s'],link=d.link_status)
        state.on_status(status)
        self.assertEqual(state.snapshot()['telemetry']['freshness'],'fresh')
        for width,height in ((80,22),(140,26)):
            for paused in (False,True):
                lines=render_lines(state.snapshot(),width,height,paused=paused)
                self.assertLessEqual(len(lines),height)
                self.assertTrue(all(cells(l.text)<=width for l in lines))
                self.assertIn('HELD' if paused else 'LIVE','\n'.join(l.text for l in lines))
        wall[0]=4
        self.assertEqual(state.snapshot()['telemetry']['freshness'],'STALE')
        self.assertTrue(all(c['freshness']=='UNSEEN' for c in state.snapshot()['channels']))


if __name__=='__main__':unittest.main()
