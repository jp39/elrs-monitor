"""Focused adaptive-link transitions; PHY-switch RF validation is separate."""
import unittest

from elrs_monitor.adaptive import AdaptiveTrackingDecoder
from elrs_monitor.profiles import get_profile
from elrs_monitor.fhss import HopClock
from elrs_monitor.model import MonitorState
from test_modes import config, sync_packet, rc_packet, seal, observation


def sync(physical, cfg, time, nonce=1, advertised=None, mode=None):
    raw = bytearray(sync_packet(physical,cfg,nonce))
    if advertised is not None:raw[3]=advertised.rate_enum
    if mode is not None:raw[4]=(raw[4]&~1)|mode
    return dict(observation(seal(raw,cfg),time,physical),_profile=physical.name)


def rc(decoder,cfg,time,nonce,high=False):
    b = observation(rc_packet(decoder.profile,cfg,nonce,high),time,decoder.profile)
    b['channel_hz'] = HopClock.from_sync(decoder.anchor,decoder.profile).frequency_at(time,decoder.channels)
    return dict(b,_profile=decoder.profile.name)


class AdaptiveTests(unittest.TestCase):
    def test_old_phy_announcement_needs_destination_sync_and_discards_old_jobs(self):
        old=get_profile('50');new=get_profile('250');cfg=config(old,'Wide')
        d=AdaptiveTrackingDecoder(cfg)
        d.feed(sync(old,cfg,1.))
        before=d.configuration_epoch
        event,=d.feed(sync(old,cfg,1.04,nonce=3,advertised=new,mode=1))
        self.assertEqual(event['status'],'announced_change')
        self.assertIsNone(d.anchor)
        self.assertEqual(d.phase,'reconfiguring')
        self.assertEqual(d.profile,new)
        self.assertEqual(d.configuration_epoch,before)
        self.assertEqual(len(d.scan_profiles),6)
        self.assertEqual(d.feed(sync(old,cfg,1.)),[])
        old_job = dict(observation(rc_packet(old,cfg,4),1.06,old),_profile=old.name)
        self.assertEqual(d.feed(old_job),[]) # queued old-PHY job after announcement
        self.assertEqual(d.feed(dict(observation(rc_packet(new,cfg,0),1.05,new),_profile=new.name)),[])
        d.feed(sync(new,dict(cfg,switch_mode='Hybrid'),1.08))
        self.assertEqual(d.phase,'following')
        self.assertEqual(d.configuration_epoch,before+1)
        frame,=d.feed(rc(d,cfg,1.084,2))
        self.assertEqual(frame['switch_mode'],'Hybrid')
        self.assertEqual(frame['packet_rate'],'250Hz')
        self.assertEqual(len(d.scan_profiles),1)

    def test_scan_missed_announcement_model_match_switch_confirmation_and_ambiguity(self):
        p=get_profile('50');cfg=config(p,'Wide');d=AdaptiveTrackingDecoder(cfg)
        wrong=dict(cfg,model_id=12)
        self.assertEqual(d.feed(sync(p,wrong,0.)),[])
        self.assertIsNone(d.anchor)
        d.feed(sync(p,cfg,1.))
        event=d.feed(sync(p,cfg,1.04,mode=1))[0]
        self.assertEqual(event['status'],'announced_change')
        self.assertEqual(d.feed(sync(p,cfg,1.04,mode=1)),[]) # duplicate cannot confirm
        self.assertIsNone(d.anchor)
        d.feed(sync(p,cfg,1.08,mode=1))
        self.assertEqual(d.switch_mode,'Hybrid')
        d.reset()
        full=get_profile('100');fcfg=dict(cfg,packet_rate=full.name,switch_mode='8ch')
        d.feed(sync(full,fcfg,2.)) # missed announcement, different PHY found by scan
        self.assertIsNotNone(d.anchor)
        self.assertEqual(d.mode_label,'8/12ch')
        self.assertEqual(d.switch_mode,'12ch')
        frame,=d.feed(rc(d,fcfg,2.01,2,high=True))
        self.assertEqual([u['channel'] for u in frame['rc']['channel_updates']],[1,2,3,4,9,10,11,12])
        state=MonitorState(source_label='test',model_id=11,channel_count=80,packet_rate=p.name,switch_mode='Wide')
        state.on_frame(frame)
        self.assertEqual(state.snapshot()['switch_mode_label'],'8/12ch')
        self.assertEqual(len(state.channels),12)
        self.assertEqual(state.channels[8].value,500)
        changed=dict(frame,packet_rate='250Hz',switch_mode='Hybrid',switch_mode_label='Hybrid',
                     configuration_epoch=frame['configuration_epoch']+1)
        changed.pop('rc');state.on_frame(changed)
        self.assertTrue(all(c.value is None for c in state.channels))
        self.assertIsNone(state.armed.value)

    def test_unsupported_announcements_never_unlock_rc_and_can_recover(self):
        p=get_profile('50');cfg=config(p,'Wide');d=AdaptiveTrackingDecoder(cfg)
        d.feed(sync(p,cfg,0.5))
        d.feed(sync(p,cfg,0.54,advertised=get_profile('250'),mode=1))
        self.assertEqual(d.phase,'reconfiguring')
        d.feed(sync(p,cfg,1.)) # TX cancelled the announcement; actual old PHY is valid again
        self.assertEqual(d.phase,'following');self.assertIsNotNone(d.anchor)
        b=sync(p,cfg,1.04)
        raw=bytearray(sync_packet(p,cfg));raw[3]=0 # FLRC enum, no supported LoRa decoder
        b['payload_bins']=observation(seal(raw,cfg),1.04,p)['payload_bins']
        event,=d.feed(b)
        self.assertEqual(event['status'],'unsupported_change')
        self.assertEqual(d.phase,'unsupported');self.assertIsNone(d.anchor)
        self.assertEqual(d.feed(dict(observation(rc_packet(p,cfg,2),1.06,p),_profile=p.name)),[])
        d.reset();self.assertEqual(d.phase,'unsupported')
        d.feed(sync(p,cfg,2.))
        self.assertEqual(d.phase,'following');self.assertIsNotNone(d.anchor)


if __name__=='__main__':unittest.main()
