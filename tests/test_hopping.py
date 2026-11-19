"""FHSS/native transport tests; synthetic packets are not live-RF proof."""
import ctypes as C
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elrs_monitor.cli import parse_args
from elrs_monitor.engine import run_receiver
from elrs_monitor.fhss import (sequence,HopClock,FIRST_HZ,CHANNEL_OFFSET_HZ,
                               ACQUISITION_CENTER_HZ)
from elrs_monitor.follow import TrackingDecoder,HopFollower
from elrs_monitor.hackrf import NativeSource,Transfer,RadioBlock
from elrs_monitor.profiles import get_profile
from elrs_monitor.protocol import uid_from_phrase
from test_modes import config,sync_packet,rc_packet,observation,seal,PHRASE
from test_binding import binding_observation,binding_packet
from test_engine_cli import Observer


class SequenceTests(unittest.TestCase):
    def test_independent_compiled_firmware_240_entry_fixture(self):
        # Compiled ExpressLRS a9d4a9c FHSS.cpp and random.cpp, UID seed
        # 0x13ad5fd7. Stubs only replace unrelated platform/logging headers.
        reference=(
            '2801051c1f0b361b191d112a353437150d2f1429094a2d223e122102172b274e4725'
            '0006334c481a4d453042324108400e49263b44160c3138464f3c4b2c043f2339071e'
            '2e433d0a2024100f3a13180328232a410d4e44141c2408101a3b4d1b201511403825'
            '003512212d162b26032934373d4a2e221e3f020b1f3c453106302f49073e48044243'
            '4b171933012705091d1339362c0e47180c4c0f324f460a3a282606242a342e493d32'
            '140c17112215291b1f2b33162536314b302c1c353b0908371d023913014103421e38'
            '0f05202d473a1921463f3c40100e230b4c4a4e3e1a454d180d0a1244482f070400'
            '27434f')
        full=bytes.fromhex('16b313ad5fd3')
        self.assertEqual(bytes(sequence(full)).hex(),reference)
        self.assertEqual(sequence(full),sequence(bytes.fromhex('000013ad5fd3')))

    def test_each_block_is_a_permutation_with_fixed_sync(self):
        for uid in (bytes.fromhex('000012345678'),uid_from_phrase(PHRASE)):
            s=sequence(uid)
            for offset in (0,80,160):
                self.assertEqual(s[offset],40)
                self.assertEqual(set(s[offset:offset+80]),set(range(80)))
            self.assertTrue(all(a!=b for a,b in zip(s,s[1:]+s[:1])))
        self.assertEqual(sequence(bytes.fromhex('000012345678'))[:10],
                         tuple(bytes.fromhex('284a2e0b443d4b4e1847')))
        with self.assertRaises(ValueError):sequence(b'1234')

    def test_nonce_and_sequence_wrap_independently(self):
        c=HopClock(.1,255,239,.004,4)
        self.assertEqual(c.index_at(.1),239)
        self.assertEqual(c.index_at(.104),0)
        self.assertEqual(c.index_at(.108),0)
        self.assertEqual(c.index_at(.1+256*.004),63)
        self.assertEqual(c.index_at(.1+240*4*.004),239)
        self.assertAlmostEqual(c.next_boundary(.1),.104)
        c=HopClock(.1,1,80,.02,2)
        self.assertEqual(c.index_at(.119),80)
        self.assertEqual(c.index_at(.12),81)
        self.assertEqual(c.index_at(.08),80)


class PhysicalAndDenseTests(unittest.TestCase):
    def test_recorded_packet_hop_alignment_including_legacy_aliases(self):
        cfg=dict(binding_phrase='julian',packet_rate='50Hz',switch_mode='Wide',
                 model_match=True,model_id=0,ota_version=4)
        d=TrackingDecoder(cfg);p=d.profile
        # Packet bytes/times/FFT labels from the physical 20s capture. The last
        # two labels alias a 2446.4MHz carrier into the 10Msps capture band.
        rows=[(2.4643284711538462,'b2a0c115085fecf1',2440400000),
              (2.4843136995192308,'24000218c07f200e',2438400000),
              (2.5043139447115386,'e0000218c07f20e3',2438400000),
              (2.564314043269231,'48000218c07f2014',2436400000),
              (2.5843139903846155,'88000218c07f027f',2436400000),
              (2.6043151274038463,'c8000218c07f20b7',2442400000),
              (2.6843145865384614,'fc000218807f2079',2436400000),
              (2.704315634615385,'60000218c07f2040',2436400000)]
        clock=HopClock(rows[0][0],193,160,.02,2)
        for i,(t,packet,recorded_label) in enumerate(rows):
            slots=round((t-clock.time_s)/p.interval)
            carrier=clock.frequency_at(clock.time_s+slots*p.interval,d.channels)
            self.assertEqual(carrier-recorded_label,10000000 if i>=6 else 0)
            b=observation(bytes.fromhex(packet),t,p);b['channel_hz']=carrier
            self.assertEqual(len(d.feed(b)),1)
        self.assertEqual(d.counts['rc'],7)

    def test_followed_250hz_high_duty_cycle_still_finds_bursts(self):
        import numpy as np
        from test_modes import waveform
        from elrs_monitor.dsp import find_bursts_fast
        from elrs_monitor.profiles import RATE
        p=get_profile('250');cfg=config(p);rng=np.random.default_rng(73)
        y=(rng.normal(size=round(.016*RATE))+1j*rng.normal(size=round(.016*RATE)))*.003
        for i in range(4):
            v=waveform(rc_packet(p,cfg,i),p);start=round(i*.004*RATE)
            y[start:start+len(v)]+=v
        self.assertEqual(len(find_bursts_fast(y,p)),4)


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.p=get_profile('50');self.cfg=config(self.p)
        self.d=TrackingDecoder(self.cfg)
        self.sync=sync_packet(self.p,self.cfg,1)
        self.d.feed(observation(self.sync,.1,self.p))
        self.clock=HopClock(.1,1,0,.02,2)

    def rc(self,slot):
        b=observation(rc_packet(self.p,self.cfg,(1+slot)&255),.1+slot*.02,self.p)
        b['channel_hz']=self.clock.frequency_at(.1+slot*.02,self.d.channels)
        return b

    def test_followed_packets_roll_phase_but_do_not_fabricate_syncs(self):
        for slot in range(1,400):
            out=self.d.feed(self.rc(slot))
            self.assertEqual(len(out),1,slot)
            self.assertEqual(out[0]['inferred_nonce'],(1+slot)&255)
            self.assertNotIn('sync',out[0])
        self.assertEqual(self.d.counts['sync'],1)
        self.assertEqual(self.d.counts['rc'],399)
        self.assertAlmostEqual(self.d.anchor['time_s'],.1+399*.02)
        self.assertTrue(self.d.anchor['tracking_anchor'])

    def test_wrong_frequency_and_off_grid_cannot_roll_phase(self):
        b=self.rc(1);b['channel_hz']=2440400000
        self.assertEqual(self.d.feed(b),[])
        self.assertEqual(self.d.counts['hop_frequency_failures'],1)
        b=self.rc(1);b['time_s']+=.0002
        self.assertEqual(self.d.feed(b),[])
        self.assertEqual(self.d.counts['off_grid'],1)
        self.assertEqual(self.d.anchor['time_s'],.1)

    def test_invalid_sync_index_or_frequency_never_anchors(self):
        for index,frequency in ((255,2440400000),(0,2400400000)):
            d=TrackingDecoder(self.cfg)
            p=bytearray(self.sync);p[1]=index;p=seal(p,self.cfg)
            b=observation(p,.1,self.p);b['channel_hz']=frequency
            self.assertEqual(d.feed(b),[])
            self.assertIsNone(d.anchor)
            self.assertEqual(d.counts['hop_frequency_failures'],1)

    def test_all_profiles_follow_nonce_wrap_with_their_own_hop_intervals(self):
        from elrs_monitor.profiles import PROFILES
        for p in PROFILES.values():
            cfg=config(p,'16ch' if p.full_resolution else 'Wide')
            d=TrackingDecoder(cfg);d.feed(observation(sync_packet(p,cfg,255),.1,p))
            clock=HopClock(.1,255,0,p.interval,p.hop_interval)
            for slot in range(1,12):
                t=.1+slot*p.interval
                b=observation(rc_packet(p,cfg,(255+slot)&255),t,p)
                b['channel_hz']=clock.frequency_at(t,d.channels)
                self.assertEqual(len(d.feed(b)),1,(p.name,slot))
            self.assertEqual(d.counts['sync'],1)

    def test_uid_alone_does_not_unlock_channels(self):
        d=TrackingDecoder(self.cfg)
        self.assertEqual(d.feed(self.rc(1)),[])
        self.assertIsNone(d.anchor)

    def test_buffered_frames_keep_their_capture_metadata(self):
        d=TrackingDecoder(self.cfg)
        b=observation(rc_packet(self.p,self.cfg,0),.08,self.p)
        b.update(rx_center_hz=2440000000,capture_epoch=4)
        d.feed(b)
        b=observation(self.sync,.1,self.p)
        b.update(rx_center_hz=2440000000,capture_epoch=5)
        out=d.feed(b)
        self.assertEqual(len(out),2)
        self.assertEqual(out[0]['capture_epoch'],4)
        self.assertEqual(out[1]['capture_epoch'],5)
        self.assertEqual(d.anchor['time_s'],.1)


class FakeRadio:
    def __init__(self):
        self.now=.1;self.center_hz=ACQUISITION_CENTER_HZ;self.calls=[]
        self.finished=threading.Event()
    def radio_time(self):return self.now
    def tune(self,center):
        if self.center_hz!=center:self.calls.append(center);self.center_hz=center


class SchedulerTests(unittest.TestCase):
    def test_future_hops_and_late_deadlines_are_not_replayed(self):
        s=FakeRadio();p=get_profile('50');channels=sequence(uid_from_phrase(PHRASE))
        f=HopFollower(s,p,channels)
        f.track(dict(time_s=.1,sync=dict(nonce=1,fhss_index=0)))
        s.now=.15;f.step()
        self.assertEqual(s.calls[-1],FIRST_HZ+channels[1]*1_000_000-CHANNEL_OFFSET_HZ)
        s.now=.19;f.step()
        s.now=.31;f.step()
        self.assertEqual(s.calls[-1],FIRST_HZ+channels[5]*1_000_000-CHANNEL_OFFSET_HZ)
        self.assertEqual(f.counts['hop_deadlines_skipped'],2)
        self.assertEqual(len(s.calls),3)

    def test_50hz_uses_idle_guard_after_payload_before_next_packet(self):
        s=FakeRadio();p=get_profile('50');channels=sequence(uid_from_phrase(PHRASE))
        f=HopFollower(s,p,channels);f.track(dict(time_s=.1,sync=dict(nonce=1,fhss_index=0)))
        s.now=.111;f.step();self.assertEqual(s.calls,[])
        s.now=.1114;f.step()
        self.assertGreater(s.now,.1+p.airtime)
        self.assertLess(s.now+NativeSource.SETTLE_SECONDS+.00052,.12)
        self.assertEqual(s.calls,[FIRST_HZ+channels[1]*1000000-CHANNEL_OFFSET_HZ])

    def test_loss_returns_to_acquisition_and_does_not_reinstall_stale_anchor(self):
        s=FakeRadio();f=HopFollower(s,get_profile('50'),sequence(uid_from_phrase(PHRASE)))
        anchor=dict(time_s=.1,sync=dict(nonce=1,fhss_index=0));f.track(anchor)
        s.now=.15;f.step();s.now=.7;f.step()
        self.assertTrue(f.lost.is_set());self.assertIsNone(f.clock)
        self.assertEqual(s.center_hz,ACQUISITION_CENTER_HZ)
        f.track(anchor);self.assertIsNone(f.clock)
        f.close()

    def test_shutdown_interrupts_scheduler_wait(self):
        s=FakeRadio();f=HopFollower(s,get_profile('50'),sequence(uid_from_phrase(PHRASE))).start()
        f.close();self.assertFalse(f.thread.is_alive())

    def test_control_error_is_captured_and_scheduler_stops(self):
        s=FakeRadio();s.tune=lambda center:(_ for _ in ()).throw(RuntimeError('USB failed'))
        f=HopFollower(s,get_profile('50'),sequence(uid_from_phrase(PHRASE))).start()
        f.thread.join(timeout=1)
        self.assertIsInstance(f.error,RuntimeError);f.close()


class FakeAPI:
    def __init__(self,fail=None):self.calls=[];self.lib=self;self.fail=fail
    def call(self,name,*args):
        self.calls.append((name,args))
        if name==self.fail:raise RuntimeError('injected '+name)
        if name in ('open','open_by_serial'):args[-1]._obj.value=123
        return 0
    def hackrf_stop_rx(self,*args):return self.call('stop_rx',*args)
    def hackrf_close(self,*args):return self.call('close',*args)
    def hackrf_exit(self,*args):return self.call('exit',*args)


def transfer(source,count,valid=None):
    data=(C.c_uint8*(count*2))()
    t=Transfer(None,data,count*2,count*2 if valid is None else valid,None,None)
    return source._receive(C.pointer(t))


class TransportTests(unittest.TestCase):
    def setUp(self):self.args=parse_args(['--uid','13ad5fd3'])

    def test_receive_only_settings_and_idempotent_cleanup(self):
        api=FakeAPI();s=NativeSource(self.args,api)
        try:s.start()
        finally:s.close();s.close()
        names=[n for n,a in api.calls]
        self.assertEqual(names.count('stop_rx'),1)
        self.assertEqual(names.count('close'),1)
        self.assertEqual(names.count('exit'),1)
        values={n:a[-1] for n,a in api.calls if n.startswith('set_')}
        self.assertEqual(values['set_amp_enable'],0)
        self.assertEqual(values['set_antenna_enable'],0)
        self.assertIn('start_rx',names)
        self.assertTrue(all('tx' not in n for n in names))

    def test_partial_start_failure_cleans_device_and_library(self):
        api=FakeAPI('set_freq');s=NativeSource(self.args,api)
        with self.assertRaises(RuntimeError):s.start()
        s.close()
        self.assertEqual([n for n,a in api.calls][-2:],['close','exit'])

    def test_retune_slices_old_and_new_epochs_with_an_explicit_gap(self):
        s=NativeSource(self.args,FakeAPI())
        with patch('elrs_monitor.hackrf.time.monotonic',return_value=1):transfer(s,100000)
        with patch('elrs_monitor.hackrf.time.monotonic',side_effect=[1.002,1.003]):s.tune(2410000000)
        with patch('elrs_monitor.hackrf.time.monotonic',return_value=1.01):transfer(s,100000)
        blocks=[s.queue.get_nowait() for _ in range(3)]
        self.assertEqual([b.epoch for b in blocks],[0,0,1])
        self.assertEqual(blocks[-1].center_hz,2410000000)
        self.assertLessEqual(abs(blocks[1].end-120000),1)
        self.assertLessEqual(abs(blocks[2].start-170000),1)
        self.assertLessEqual(abs(s.retune_discarded_samples-50000),2)
        self.assertEqual(s.dropped_samples,0)
        self.assertAlmostEqual(s.retune_max_seconds,.001)

    def test_recalibration_discards_stale_timing_but_preserves_sample_counter(self):
        s=NativeSource(self.args,FakeAPI());s.samples=100000;s.last_read=1
        s.origin=-100;s.epochs[0].start=999999999
        s.recalibrate()
        self.assertAlmostEqual(s.origin,.99)
        self.assertEqual(s.samples,100000)
        self.assertEqual(s.epochs[0].start,140000)
        self.assertEqual(s.epochs[0].number,1)
        self.assertEqual(s.retunes,0)

    def test_init_failure_does_not_close_unopened_device(self):
        api=FakeAPI('init');s=NativeSource(self.args,api)
        with self.assertRaises(RuntimeError):s.start()
        s.close();self.assertEqual([n for n,a in api.calls],['init'])

    def test_callback_validation_and_bounded_queue(self):
        s=NativeSource(self.args,FakeAPI())
        for _ in range(20):self.assertEqual(transfer(s,100),0)
        self.assertEqual(s.queue.qsize(),6)
        self.assertEqual(s.dropped_samples,1400)
        self.assertEqual(s.retune_discarded_samples,0)
        self.assertEqual(transfer(s,100,valid=199),-1)
        self.assertTrue(s.finished.is_set());self.assertIsInstance(s.error,RuntimeError)
        s=NativeSource(self.args,FakeAPI());bad=Transfer(None,None,2,2,None,None)
        self.assertEqual(s._receive(C.pointer(bad)),-1)

    def test_duration_limit_and_cancellation(self):
        self.args.seconds=.001;s=NativeSource(self.args,FakeAPI())
        self.assertEqual(transfer(s,20000),-1)
        self.assertTrue(s.finished.is_set())
        self.assertEqual(len(s.queue.get_nowait().data)//2,10000)
        s.stop.set();self.assertEqual(transfer(s,100),-1)


class LiveEngineTests(unittest.TestCase):
    def run_fake(self,normal=True,log=None,stop=None):
        p=get_profile('50');cfg=config(p,'Wide',model_id=0)
        uid=uid_from_phrase(PHRASE);channels=sequence(uid)
        next_center=FIRST_HZ+channels[1]*1_000_000-CHANNEL_OFFSET_HZ
        class Source:
            def __init__(self,args):
                self.queue=queue.Queue(maxsize=6)
                self.stop=threading.Event();self.finished=threading.Event();self.finished.set()
                self.samples=3_000_000;self.last_read=10**20
                self.dropped_samples=self.retunes=self.retune_discarded_samples=0
                self.retune_max_seconds=0;self.center_hz=ACQUISITION_CENTER_HZ;self.error=None
                self.starts=self.closes=0
                for i in range(3):self.queue.put(RadioBlock(i*1_000_000,bytes(2_000_000),
                    next_center if i==2 and normal else ACQUISITION_CENTER_HZ,1 if i==2 and normal else 0))
            def start(self):self.starts+=1;return self
            def close(self):self.closes+=1
            def radio_time(self):return .3
        sources=[]
        def create(args):s=Source(args);sources.append(s);return s
        def process(w,center,epoch,target,binding):
            out=[binding_observation(t,binding_packet(uid)) for t in (.095,.135,.175)]
            if normal:
                out.append(observation(sync_packet(p,cfg,1),.191,p))
                rc=observation(rc_packet(p,cfg,2),.211,p)
                rc['channel_hz']=next_center+CHANNEL_OFFSET_HZ
                out.append(rc)
            return [dict(b,rx_center_hz=center,capture_epoch=epoch) for b in out
                    if w.core_start/1e7 <= b['time_s'] < w.core_end/1e7]
        flags=['--discover-binding','--rate','50','--switch-mode','Wide','--model-match','on','--model-id','0']
        if log:flags+=['--jsonl',str(log)]
        args=parse_args(flags);observer=Observer()
        with patch('elrs_monitor.live.NativeSource',side_effect=create),patch('elrs_monitor.live.process_radio_window',side_effect=process):
            run_receiver(args,dict(discover_binding=True,packet_rate=p.name,switch_mode='Wide',
                                 model_match=True,model_id=0,ota_version=4),observer,stop)
        return sources[0],observer

    def test_default_live_replacement_handover_crosses_frequency_epochs(self):
        with tempfile.TemporaryDirectory() as directory:
            log=Path(directory)/'frames.jsonl';source,obs=self.run_fake(log=log)
            self.assertEqual((source.starts,source.closes),(1,1))
            self.assertEqual(len(obs.frames),5)
            self.assertIn('sync',obs.frames[3]);self.assertIn('rc',obs.frames[4])
            self.assertEqual(obs.frames[4]['capture_epoch'],1)
            self.assertEqual(obs.frames[4]['rx_center_hz']+CHANNEL_OFFSET_HZ,obs.frames[4]['channel_hz'])
            status=obs.statuses[-1]
            self.assertEqual(status['phase'],'following');self.assertTrue(status['locked'])
            self.assertEqual(status['capture_mode'],'hop-following')
            self.assertEqual(status['counters']['handovers'],1)
            self.assertEqual([json.loads(s) for s in log.read_text().splitlines()],obs.frames)

    def test_discovery_without_normal_sync_does_not_follow(self):
        source,obs=self.run_fake(normal=False)
        self.assertEqual(len(obs.frames),3)
        self.assertFalse(obs.statuses[-1]['locked'])
        self.assertEqual(obs.statuses[-1]['phase'],'awaiting_sync')
        self.assertEqual(obs.statuses[-1]['identity']['source'],'binding')

    def test_frequency_epoch_iq_goes_through_real_dsp_and_nonce_validation(self):
        import numpy as np
        from scipy.signal import resample_poly
        from test_modes import waveform
        from elrs_monitor.cli import link_settings
        from elrs_monitor.constants import FS
        p=get_profile('50');cfg=config(p,'Wide',model_id=0);channels=sequence(uid_from_phrase(PHRASE))
        rng=np.random.default_rng(99);count=2_000_000
        x=(rng.normal(size=count)+1j*rng.normal(size=count))*.002
        for nonce in range(10):
            packet=sync_packet(p,cfg,nonce) if nonce==1 else rc_packet(p,cfg,nonce)
            v=resample_poly(np.r_[np.zeros(256),waveform(packet,p),np.zeros(p.n*2)],80,13)
            start=round(nonce*p.interval*FS)
            x[start:start+len(v)]+=.45*v
        x*=np.exp(2j*np.pi*400000*np.arange(count)/FS)
        raw=np.clip(np.c_[x.real,x.imag]*110,-128,127).astype(np.int8).tobytes()
        s=SimpleNamespace(queue=queue.Queue(maxsize=100),stop=threading.Event(),finished=threading.Event(),
                          samples=count,last_read=10**20,dropped_samples=0,retunes=4,
                          retune_discarded_samples=160000,retune_max_seconds=.0005,
                          center_hz=ACQUISITION_CENTER_HZ,error=None)
        s.finished.set();s.start=lambda:s;s.close=lambda:None
        for epoch in range(5):
            lo=epoch*400000+(40000 if epoch else 0);hi=(epoch+1)*400000
            center=FIRST_HZ+channels[epoch]*1000000-CHANNEL_OFFSET_HZ
            for start in range(lo,hi,131072):
                end=min(start+131072,hi)
                s.queue.put(RadioBlock(start,raw[start*2:end*2],center,epoch))
        args=parse_args(['--bind-phrase',PHRASE,'--rate','50','--switch-mode','Wide',
                         '--model-match','on','--model-id','0'])
        obs=Observer()
        with patch('elrs_monitor.live.NativeSource',return_value=s):
            run_receiver(args,link_settings(args),obs)
        self.assertEqual(len(obs.frames),6)
        rc=[f for f in obs.frames if 'rc' in f]
        self.assertEqual([f['inferred_nonce'] for f in rc],[0,3,5,7,9])
        self.assertTrue(all(f['rc']['channels_raw10']==[100,200,300,400] for f in rc))
        self.assertEqual(obs.statuses[-1]['phase'],'following')

    def test_existing_live_log_is_not_overwritten_or_hardware_opened(self):
        from elrs_monitor.cli import link_settings
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'keep.jsonl';path.write_text('keep')
            args=parse_args(['--uid','13ad5fd3','--jsonl',str(path)])
            obs=Observer()
            with patch('elrs_monitor.hackrf.NativeSource.start') as start:
                with self.assertRaises(FileExistsError):run_receiver(args,link_settings(args),obs)
                start.assert_not_called()
            self.assertEqual(path.read_text(),'keep');self.assertTrue(obs.statuses[-1]['done'])

    def test_live_gap_does_not_duplicate_the_new_core(self):
        from elrs_monitor.cli import link_settings
        s=SimpleNamespace(queue=queue.Queue(maxsize=6),stop=threading.Event(),finished=threading.Event(),
                          samples=80000,last_read=10**20,dropped_samples=0,retunes=0,
                          retune_discarded_samples=0,retune_max_seconds=0,
                          center_hz=ACQUISITION_CENTER_HZ,error=None)
        s.finished.set();s.start=lambda:s;s.close=lambda:None
        for start in (0,50000):s.queue.put(RadioBlock(start,bytes(60000)))
        seen=[]
        def process(w,*args):seen.append(w.core_start);return []
        args=parse_args(['--uid','13ad5fd3'])
        with patch('elrs_monitor.live.NativeSource',return_value=s),patch('elrs_monitor.live.process_radio_window',side_effect=process):
            run_receiver(args,link_settings(args),Observer())
        self.assertEqual(seen,[0,50000])

    def test_cancel_before_start_never_opens_radio(self):
        stop=threading.Event();stop.set()
        source,obs=self.run_fake(stop=stop)
        self.assertEqual(source.starts,0);self.assertEqual(source.closes,1)
        self.assertTrue(obs.statuses[-1]['done'])
