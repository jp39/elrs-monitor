"""Passive discovery and direct UID tests, with synthetic and physical RF fixtures."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy.signal import resample_poly

from elrs_monitor.binding import BindingDiscoveryDecoder, MAX_CANDIDATES
from elrs_monitor.cli import parse_args, link_settings, main, format_frame
from elrs_monitor.decoder import LiveDecoder, validate_config, identity_details
from elrs_monitor.model import MonitorState
from elrs_monitor.profiles import get_profile
from elrs_monitor.protocol import crc14, parse_uid, format_uid, uid_from_phrase
from elrs_monitor.tui import render_lines
from test_modes import (PHRASE, config, encode, observation, waveform,
                        sync_packet, rc_packet)

PROFILE = get_profile('50Hz')
UID = bytes.fromhex('000012345678')
DISCOVERY = dict(discover_binding=True,packet_rate='50Hz',switch_mode='Hybrid',
                 ota_version=4,model_match=False,model_id=0)


def binding_packet(uid=UID,*,kind=1,index=1,command=9,nonce=0):
    # ELRS4 std data_ul = type/CRC high, packageIndex/ack, five payload bytes,
    # CRC low. Binding CRC seed is the bare OTA version, 4, nonce locked at 0.
    p=bytearray([kind,index,command,*uid[-4:],0])
    value=crc14(p[:7],4^nonce)
    p[0]|=(value>>8)<<2;p[7]=value&255
    return bytes(p)


def binding_observation(t,packet=None,polarity=1):
    b=observation(binding_packet() if packet is None else packet,t,PROFILE)
    if polarity==-1:
        b['polarity']=-1
        b['payload_bins']=[(-v)%PROFILE.bins for v in b['payload_bins']]
    return b


def write_iq(path,packets,duration=.3):
    """Known synthetic 400kHz-offset signal; caller controls all packet bytes."""
    fs=10_000_000
    x=np.zeros(round(duration*fs),dtype=np.complex64)
    for entry in packets:
        t,p=entry[:2]
        profile=entry[2] if len(entry)==3 else PROFILE
        v=resample_poly(np.r_[np.zeros(256),waveform(p,profile),np.zeros(profile.n*2)],80,13)
        start=round(t*fs)
        x[start:start+len(v)]=.3*v
    x*=np.exp(2j*np.pi*400000*np.arange(len(x))/fs)
    raw=np.empty(len(x)*2,dtype=np.int8)
    raw[::2]=np.rint(x.real*128);raw[1::2]=np.rint(x.imag*128)
    raw+=np.random.default_rng(97).integers(-1,2,len(raw),dtype=np.int8)
    path.write_bytes(raw.tobytes())


class IdentityTests(unittest.TestCase):
    def test_uid_formats_and_four_byte_prefix(self):
        for text in ('12:34:56:78','12345678','12-34-56-78',
                     '00:00:12:34:56:78','000012345678','00-00-12-34-56-78'):
            self.assertEqual(parse_uid(text),UID)
        self.assertEqual(parse_uid('AB:CD:12:34:56:78'),bytes.fromhex('abcd12345678'))
        self.assertEqual(format_uid(bytes.fromhex('12345678')),'00:00:12:34:56:78')

    def test_invalid_uids(self):
        for value in ('','123456','123456789','gg:34:56:78','1:34:56:78',
                      '12:34-56:78','12 34 56 78','0x12345678','00000000',
                      '11:22:00:00:00:00',None,123,True,b'123'):
            with self.subTest(value=value),self.assertRaises(ValueError):parse_uid(value)

    def test_phrase_and_uid_have_same_link_validation(self):
        cfg=config(PROFILE)
        uid=uid_from_phrase(PHRASE)
        for raw in (uid,uid[2:],format_uid(uid)):
            direct={k:v for k,v in cfg.items() if k!='binding_phrase'}
            direct['uid']=raw
            d=LiveDecoder(direct)
            self.assertEqual(d.initializer,LiveDecoder(cfg).initializer)
            frames=d.feed(observation(sync_packet(PROFILE,cfg,1),.02,PROFILE))
            frames+=d.feed(observation(rc_packet(PROFILE,cfg,2),.04,PROFILE))
            self.assertEqual(len(frames),2)
            self.assertTrue(all(f['crc_ok'] for f in frames))

    def test_display_identity_distinguishes_base_and_sync_uid(self):
        cfg=dict(binding_phrase='julian',model_match=True,model_id=0)
        identity=identity_details(cfg)
        self.assertEqual(identity['binding_phrase'],'julian')
        self.assertEqual(identity['uid'],'16:b3:13:ad:5f:d3')
        self.assertEqual(identity['link_uid'],'13:ad:5f:d3')
        self.assertEqual(identity['sync_uid'],'5f:ec')
        off=identity_details({**cfg,'model_match':False})
        self.assertEqual(off['uid'],identity['uid'])
        self.assertEqual(off['sync_uid'],'5f:d3')
        direct=identity_details(dict(uid=identity['uid'],model_match=True,model_id=0))
        self.assertEqual(direct['uid'],identity['uid'])
        self.assertEqual(direct['sync_uid'],identity['sync_uid'])
        self.assertEqual(direct['source'],'uid')
        self.assertIsNone(identity_details(DISCOVERY))

    def test_exactly_one_cli_identity(self):
        for args in ([],['--uid','bad'],['--uid','12345678','--bind-phrase',PHRASE],
                     ['--discover-binding','--uid','12345678'],
                     ['--discover-binding','--bind-phrase',PHRASE]):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):parse_args(args)
        cfg=link_settings(parse_args(['--uid','12345678']))
        self.assertEqual(cfg['uid'],UID)
        self.assertNotIn('binding_phrase',cfg)
        cfg=link_settings(parse_args(['--discover-binding']))
        self.assertEqual(cfg,{**DISCOVERY,'packet_rate':'250Hz'})
        args=parse_args(['--discover-binding'])
        self.assertEqual(args.packet_rate,'250Hz')
        self.assertIsNone(args.seconds)
        self.assertEqual(len(args.offset),9)
        self.assertIsNone(args.binding_phrase)

    def test_discovery_options_checked_before_hardware(self):
        for flags in (['--rate','333'],['--model-id','64'],['--offset','-600000']):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                parse_args(['--discover-binding',*flags])
        args=parse_args(['--discover-binding','--rate','50','--switch-mode','Wide',
                         '--model-match','on','--model-id','11','--seconds','5'])
        self.assertEqual(args.seconds,5)
        self.assertEqual(args.switch_mode,'Wide')
        self.assertTrue(args.model_match)
        args=parse_args(['--discover-binding','--rate','333','--switch-mode','16ch'])
        self.assertEqual(args.packet_rate,'333Hz Full')

    def test_library_identity_validation(self):
        for cfg in ({**DISCOVERY,'uid':UID},{**DISCOVERY,'binding_phrase':PHRASE},
                    {**config(PROFILE),'uid':UID},{**DISCOVERY,'model_id':64}):
            with self.assertRaises(ValueError):validate_config(cfg)
        with self.assertRaises(ValueError):LiveDecoder(DISCOVERY)


class DiscoveryTests(unittest.TestCase):
    def test_binding_crc_fixture_from_firmware(self):
        # Independently compiled ELRS 4.1.0 Crc2Byte::calc, 14/0x2e57,
        # seed=4, nonce=0: bytes 01 01 09 12 34 56 78 -> CRC 0x00dd.
        # This validates OTA layout/CRC, not the synthetic LoRa modulation.
        packet=bytes.fromhex('01010912345678dd')
        self.assertEqual(binding_packet(),packet)
        d=BindingDiscoveryDecoder(DISCOVERY)
        for t in (.02,.06,.10):d.feed(binding_observation(t,packet))
        self.assertEqual(d.confirmed_uid,UID)

    def test_physical_binding_capture_symbol_fixture(self):
        # Independently recorded 50Hz binding DATA repeated at 40ms intervals.
        # No synthetic encoder is used for these actual SF8 modulation bins.
        bins=[161,217,73,181,33,181,181,5,183,138,54,96,213,5,88,212,54,95]
        d=BindingDiscoveryDecoder(DISCOVERY)
        for t in (10.4910564904,10.5310475793,10.5710479519):
            frames=d.feed(dict(time_s=t,channel_hz=2_440_400_000,polarity=1,payload_bins=bins))
            self.assertEqual(frames[0]['packet_hex'],'b5010913ad5fd33f')
            self.assertTrue(frames[0]['crc_ok'])
        identity=identity_details(dict(binding_phrase='julian',model_match=True,model_id=0))
        self.assertEqual(d.confirmed_uid[2:].hex(':'),identity['link_uid'])
        self.assertEqual(format_uid(d.confirmed_uid),'00:00:13:ad:5f:d3')

    def test_repeated_binding_without_sync_both_polarities(self):
        for polarity in (1,-1):
            d=BindingDiscoveryDecoder(DISCOVERY)
            for i,t in enumerate((.02,.06,.10),1):
                frames=d.feed(binding_observation(t,polarity=polarity))
                self.assertEqual(len(frames),1)
                f=frames[0]
                self.assertEqual(f['binding']['confirmations'],i)
                self.assertEqual(f['binding']['confirmed'],i==3)
                self.assertEqual(f['binding']['uid'],'00:00:12:34:56:78')
                self.assertEqual(f['binding']['uid_bytes_received'],4)
                self.assertNotIn('rc',f)
                self.assertNotIn('sync',f)
            self.assertIsNone(d.anchor)
            self.assertEqual(d.confirmed_uid,UID)
            self.assertEqual(d.feed(binding_observation(.14)),[])
            self.assertIn('DISCOVERED',format_frame(f))

    def test_duplicates_are_not_independent_repeats(self):
        d=BindingDiscoveryDecoder(DISCOVERY)
        d.feed(binding_observation(.02))
        self.assertEqual(d.feed(binding_observation(.02)),[])
        self.assertEqual(d.feed(binding_observation(.0201,polarity=-1)),[])
        self.assertEqual(d.confirmations,1)
        self.assertIsNone(d.confirmed_uid)

    def test_reject_wrong_crc_nonce_type_command_index_channel_and_zero_uid(self):
        bad_crc=bytearray(binding_packet());bad_crc[3]^=1
        bad=[bytes(bad_crc),binding_packet(nonce=1),binding_packet(kind=0),
             binding_packet(command=10),binding_packet(index=2),binding_packet(uid=bytes(6))]
        for packet in bad:
            d=BindingDiscoveryDecoder(DISCOVERY)
            self.assertEqual(d.feed(binding_observation(.02,packet)),[])
            self.assertIsNone(d.confirmed_uid)
        d=BindingDiscoveryDecoder(DISCOVERY)
        b=binding_observation(.02);b['channel_hz']-=1_000_000
        self.assertEqual(d.feed(b),[])
        b=binding_observation(.04);b['payload_bins']=[]
        self.assertEqual(d.feed(b),[])
        self.assertEqual(d.counts['fec_failures'],1)

    def test_normal_50hz_traffic_is_not_binding_discovery(self):
        d=BindingDiscoveryDecoder(DISCOVERY);cfg=config(PROFILE)
        for i in range(1,7):
            data=sync_packet(PROFILE,cfg,i) if i%2 else rc_packet(PROFILE,cfg,i)
            self.assertEqual(d.feed(observation(data,i*.02,PROFILE)),[])
        self.assertIsNone(d.confirmed_uid)

    def test_expiry_and_gap_reset_do_not_reuse_confirmations(self):
        d=BindingDiscoveryDecoder(DISCOVERY)
        d.feed(binding_observation(.02));d.feed(binding_observation(.06))
        d.advance(2.1)
        self.assertEqual(d.confirmations,0)
        d.feed(binding_observation(2.2))
        self.assertEqual(d.confirmations,1)
        d.reset()
        self.assertEqual(d.confirmations,0)
        self.assertEqual(d.counts['resets'],1)
        d.feed(binding_observation(2.3))
        self.assertEqual(d.confirmations,1)

    def test_multiple_uids_not_combined_and_candidates_bounded(self):
        d=BindingDiscoveryDecoder(DISCOVERY)
        for i in range(MAX_CANDIDATES+2):
            uid=(i+1).to_bytes(4,'big')
            d.feed(binding_observation(i*.02,binding_packet(uid)))
        self.assertEqual(len(d.candidates),MAX_CANDIDATES)
        self.assertEqual(d.confirmations,1)
        self.assertIsNone(d.confirmed_uid)
        self.assertEqual(d.counts['binding_evictions'],2)

    def test_discovery_tui_never_shows_rc_or_fake_lock(self):
        d=BindingDiscoveryDecoder(DISCOVERY)
        state=MonitorState(source_label='TEST',model_id=0,model_match=False,channel_count=9,
                           packet_rate='50Hz',discover_binding=True)
        self.assertIn('LISTENING FOR BIND','\n'.join(l.text for l in render_lines(state.snapshot(),100,24)))
        for t in (.02,.06,.10):
            for f in d.feed(binding_observation(t)):state.on_frame(f)
        status=state.snapshot()['status']
        identity=dict(identity_details(dict(uid=d.confirmed_uid,model_match=False,model_id=0)),source='binding')
        status.update(discovered_uid=format_uid(d.confirmed_uid),binding_confirmations=3,
                      discover_binding=False,identity=identity,phase='awaiting_sync')
        state.on_status(status)
        text='\n'.join(l.text for l in render_lines(state.snapshot(),100,24))
        self.assertIn('WAITING NORMAL SYNC',text)
        self.assertIn('UID 00:00:12:34:56:78',text)
        self.assertNotIn('SYNC LOCKED',text)
        self.assertNotIn('DISARMED',text)
        self.assertTrue(all(c['value'] is None for c in state.snapshot()['channels']))


class ReplayTests(unittest.TestCase):
    def test_discovery_cli_synthetic_iq_and_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            iq=Path(directory)/'binding.iq';log=Path(directory)/'binding.jsonl'
            write_iq(iq,[(t,binding_packet()) for t in (.095,.135,.175)])
            stdout=io.StringIO();stderr=io.StringIO()
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=main(['--discover-binding','--replay',str(iq),'--format','json',
                           '--jsonl',str(log),'--offset','400000'])
            self.assertEqual(code,0,stderr.getvalue())
            frames=[json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(len(frames),3)
            self.assertEqual([f['binding']['confirmed'] for f in frames],[False,False,True])
            self.assertEqual(frames[-1]['status'],'binding_discovered')
            self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],frames)
            self.assertEqual(log.stat().st_mode&0o777,0o600)
            self.assertIn('"discovered_uid": "00:00:12:34:56:78"',stderr.getvalue())
            self.assertIn('"locked": false',stderr.getvalue())

    def test_dense_50hz_binding_repetitions_are_detected(self):
        # Real Bind mode alternates DATA and RC on one channel. Its >50%
        # duty cycle breaks median-based noise-floor detection, unlike sparse
        # synthetic DATA-only fixtures. RC must not count as confirmation.
        with tempfile.TemporaryDirectory() as directory:
            iq=Path(directory)/'dense-binding.iq'
            packets=[(.095+i*.02,binding_packet(kind=1 if i%2==0 else 0)) for i in range(10)]
            write_iq(iq,packets,duration=.4)
            stdout=io.StringIO();stderr=io.StringIO()
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=main(['--discover-binding','--replay',str(iq),'--format','json'])
            self.assertEqual(code,0,stderr.getvalue())
            frames=[json.loads(s) for s in stdout.getvalue().splitlines()]
            self.assertEqual(len(frames),3)
            self.assertTrue(frames[-1]['binding']['confirmed'])
            self.assertTrue(all(f['packet_type']==1 and 'rc' not in f for f in frames))
            self.assertLess(frames[-1]['time_s']-frames[0]['time_s'],.081)

    def test_replay_discovery_uid_survives_gap(self):
        from elrs_monitor.capture import Block
        from elrs_monitor.engine import run_receiver
        from test_engine_cli import Observer
        sources=[]
        def start(source):
            sources.append(source)
            source.samples=4_000_000
            for core in (0,1_000_000,3_000_000):
                source.queue.put(Block(core,bytes(2_000_000)))
            source.queue.put(None)
            return source
        frames=[binding_observation(t) for t in (.02,.06,.10)]
        observer=Observer();args=parse_args(['--discover-binding','--rate','50','--replay','/mock.iq'])
        def process(window,offsets,profile):
            self.assertFalse(sources[0].stop.is_set(),'Capture must not stop at UID discovery')
            return frames if window.core_start==0 else []
        with patch('elrs_monitor.engine.Source.start',new=start), \
             patch('elrs_monitor.engine.process_window',side_effect=process):
            run_receiver(args,DISCOVERY,observer)
        self.assertEqual(len(observer.frames),3)
        status=observer.statuses[-1]
        self.assertEqual(status['discovered_uid'],'00:00:12:34:56:78')
        self.assertTrue(status['done'])
        self.assertFalse(status['discover_binding'])
        self.assertEqual(status['binding_confirmations'],3)
        self.assertEqual(status['counters']['handovers'],1)
        self.assertGreaterEqual(status['counters']['resets'],1)
        self.assertEqual(status['phase'],'awaiting_sync')

    def test_discovery_eof_without_confirmation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            iq=Path(directory)/'short.iq'
            write_iq(iq,[(.095,binding_packet()),(.135,binding_packet())],duration=.2)
            stdout=io.StringIO();stderr=io.StringIO()
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=main(['--discover-binding','--replay',str(iq),'--format','json','--offset','400000'])
            self.assertEqual(code,1)
            self.assertIn('No UID discovered',stderr.getvalue())
            self.assertNotIn('binding_discovered',stdout.getvalue())

    def test_binding_handover_to_same_and_different_profiles_without_losing_core(self):
        # The final binding packet and normal SF5/SF6 packets share a 100ms
        # core. Also exercise already queued same-profile (50Hz) jobs.
        for rate,mode in (('50','Wide'),('250','Hybrid'),('333','16ch')):
            target=get_profile(rate);cfg=config(target,mode,model_id=0)
            uid=uid_from_phrase(PHRASE)
            with self.subTest(rate=rate),tempfile.TemporaryDirectory() as directory:
                iq=Path(directory)/'handover.iq';log=Path(directory)/'session.jsonl'
                packets=[(t,binding_packet(uid)) for t in (.095,.135,.175)]
                packets += [(.191,sync_packet(target,cfg,1),target),
                            (.191+target.interval,rc_packet(target,cfg,2),target)]
                write_iq(iq,packets)
                stdout=io.StringIO();stderr=io.StringIO()
                with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                    code=main(['--discover-binding','--rate',rate,'--switch-mode',mode,
                               '--model-match','on','--model-id','0','--replay',str(iq),
                               '--format','json','--jsonl',str(log),'--offset','400000'])
                self.assertEqual(code,0,stderr.getvalue())
                frames=[json.loads(s) for s in stdout.getvalue().splitlines()]
                self.assertEqual(len(frames),5)
                self.assertTrue(frames[2]['binding']['confirmed'])
                self.assertIn('sync',frames[3]);self.assertIn('rc',frames[4])
                self.assertEqual(frames[4]['inferred_nonce'],2)
                self.assertEqual(frames[4]['rc']['switch_mode'],mode)
                self.assertEqual([json.loads(s) for s in log.read_text().splitlines()],frames)
                final=json.loads(stderr.getvalue().split('summary: ')[-1])
                self.assertEqual(final['phase'],'monitoring')
                self.assertTrue(final['locked'])
                self.assertEqual(final['counters']['handovers'],1)
                self.assertEqual(final['identity']['source'],'binding')
                self.assertEqual(final['binding_confirmations'],3)
                self.assertFalse(final['discover_binding'])

    def test_handover_requires_normal_sync_and_rejects_wrong_model_or_switch(self):
        uid=uid_from_phrase(PHRASE)
        for wrong in ('no_sync','model','switch'):
            actual=config(PROFILE,'Hybrid' if wrong=='switch' else 'Wide',model_id=11 if wrong=='model' else 0)
            packets=[(t,binding_packet(uid)) for t in (.095,.135,.175)]
            if wrong!='no_sync':packets.append((.235,sync_packet(PROFILE,actual,1)))
            packets.append((.255,rc_packet(PROFILE,actual,2)))
            with self.subTest(case=wrong),tempfile.TemporaryDirectory() as directory:
                iq=Path(directory)/'mismatch.iq';write_iq(iq,packets)
                stdout=io.StringIO();stderr=io.StringIO()
                with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                    code=main(['--discover-binding','--rate','50','--switch-mode','Wide',
                               '--model-match','on','--model-id','0','--replay',str(iq),
                               '--format','json','--offset','400000'])
                self.assertEqual(code,0,stderr.getvalue())
                frames=[json.loads(s) for s in stdout.getvalue().splitlines()]
                self.assertEqual(len(frames),3)
                self.assertTrue(all('rc' not in f and 'sync' not in f for f in frames))
                final=json.loads(stderr.getvalue().split('summary: ')[-1])
                self.assertEqual(final['phase'],'awaiting_sync')
                self.assertFalse(final['locked'])
                if wrong=='model':self.assertGreater(final['counters'].get('identity_failures',0),0)
                if wrong=='switch':self.assertGreater(final['counters'].get('unsupported_sync',0),0)

    def test_normal_traffic_before_uid_is_not_used_for_monitoring(self):
        uid=uid_from_phrase(PHRASE);cfg=config(PROFILE,'Wide',model_id=0)
        with tempfile.TemporaryDirectory() as directory:
            iq=Path(directory)/'before.iq'
            packets=[(.035,sync_packet(PROFILE,cfg,1)),(.055,rc_packet(PROFILE,cfg,2))]
            packets += [(t,binding_packet(uid)) for t in (.095,.135,.175)]
            packets += [(.235,sync_packet(PROFILE,cfg,1)),(.255,rc_packet(PROFILE,cfg,2))]
            write_iq(iq,packets)
            stdout=io.StringIO();stderr=io.StringIO()
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=main(['--discover-binding','--rate','50','--switch-mode','Wide',
                           '--model-match','on','--model-id','0','--replay',str(iq),
                           '--format','json','--offset','400000'])
            self.assertEqual(code,0,stderr.getvalue())
            frames=[json.loads(s) for s in stdout.getvalue().splitlines()]
            self.assertEqual(len(frames),5)
            self.assertTrue(all(f['time_s']>.23 for f in frames if 'sync' in f or 'rc' in f))

    def test_rc_gap_metrics_use_sample_timestamps_not_replay_wall_time(self):
        cfg=config(PROFILE)
        with tempfile.TemporaryDirectory() as directory:
            iq=Path(directory)/'gaps.iq'
            write_iq(iq,[(.095,sync_packet(PROFILE,cfg,1)),(.115,rc_packet(PROFILE,cfg,2)),
                         (.135,rc_packet(PROFILE,cfg,3)),(.255,rc_packet(PROFILE,cfg,9))])
            stdout=io.StringIO();stderr=io.StringIO()
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=main(['--bind-phrase',PHRASE,'--rate','50','--model-match','on','--model-id','11',
                           '--replay',str(iq),'--format','json','--offset','400000'])
            self.assertEqual(code,0,stderr.getvalue())
            final=json.loads(stderr.getvalue().split('summary: ')[-1])
            self.assertAlmostEqual(final['last_rc_gap_seconds'],.12,places=5)
            self.assertAlmostEqual(final['max_rc_gap_seconds'],.12,places=5)
            self.assertEqual(final['processing_lag_seconds'],0)

    def test_normal_50hz_uid_cli_replay(self):
        cfg=config(PROFILE)
        with tempfile.TemporaryDirectory() as directory:
            iq=Path(directory)/'normal.iq'
            write_iq(iq,[(.095,sync_packet(PROFILE,cfg,1)),(.115,rc_packet(PROFILE,cfg,2))])
            stdout=io.StringIO();stderr=io.StringIO()
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=main(['--uid',uid_from_phrase(PHRASE)[2:].hex(),'--rate','50',
                           '--model-match','on','--model-id','11','--replay',str(iq),
                           '--format','json','--offset','400000'])
            self.assertEqual(code,0,stderr.getvalue())
            frames=[json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(len(frames),2)
            self.assertIn('sync',frames[0]);self.assertIn('rc',frames[1])
            self.assertEqual(frames[1]['inferred_nonce'],2)
            self.assertIn('Explicit UID -> base UID',stderr.getvalue())


if __name__=='__main__':unittest.main()
