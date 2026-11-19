"""Profile/OTA extensions: synthetic tests, not independent RF validation."""
import contextlib
import io
import json
import math
import tempfile
from pathlib import Path
import unittest

import numpy as np
from scipy.signal import resample_poly

from elrs_monitor.cli import parse_args, link_settings, main
from unittest.mock import patch
from elrs_monitor.decoder import LiveDecoder, validate_config
from elrs_monitor.model import MonitorState
from elrs_monitor.tui import render_lines
from elrs_monitor.phy import chirp_reference, recover_payload
from elrs_monitor.dsp import extract_window
from elrs_monitor.profiles import PROFILES, DEFAULT, RATE, get_profile, switch_bit
from elrs_monitor.protocol import (CODEBOOK, WHITENING, crc, uid_from_phrase,
                                  decode_long_interleaver, decode_rc, validate_crc)


PHRASE = 'synthetic-profile-tests-only'


def config(profile=DEFAULT,mode='Hybrid',model_match=True,model_id=11):
    return dict(binding_phrase=PHRASE,packet_rate=profile.name,switch_mode=mode,
                model_match=model_match,model_id=model_id,ota_version=4)


def seal(payload,cfg,nonce=0):
    p=bytearray(payload)
    uid=uid_from_phrase(cfg['binding_phrase'])
    seed=((uid[4]<<8)|uid[5])^0x400
    if len(p)==8:
        p[0]&=3
        value=crc(p[:7],seed^nonce,14,0x2e57)
        p[0]|=(value>>8)<<2;p[7]=value&255
    else:
        value=crc(p[:11],seed^nonce,16,0x3d65)
        p[11:13]=value.to_bytes(2,'little')
    return bytes(p)


def sync_packet(profile,cfg,nonce=1):
    uid=uid_from_phrase(cfg['binding_phrase'])
    p=bytearray(profile.packet_bytes)
    p[:7]=bytes([2,0,nonce,profile.rate_enum,switch_bit(cfg['switch_mode']),uid[4],
                 uid[5]^((~cfg['model_id'])&63 if cfg['model_match'] else 0)])
    return seal(p,cfg)


def pack_four(values):
    return sum(v<<(10*i) for i,v in enumerate(values)).to_bytes(5,'little')


def rc_packet(profile,cfg,nonce=2,high=False):
    if profile.full_resolution:
        p=bytes([128|(64 if high else 0)])+pack_four([100,200,300,400])+pack_four([500,600,700,800])+b'\0\0'
    else:
        p=bytes([0])+pack_four([100,200,300,400])+bytes([0x80|7,0])
    return seal(p,cfg,nonce)


def encode(data,profile,flip=()):
    white=[d^w for d,w in zip(data,WHITENING)]
    nibbles=[v for b in white for v in (b&15,b>>4)]
    words=CODEBOOK[nibbles]>>(8-profile.code_bits)
    stream=((words[None,:]>>np.arange(profile.code_bits-1,-1,-1)[:,None])&1).ravel()
    for i in flip:stream[i]^=1
    out=[];at=0
    for width in profile.symbol_widths:
        bits=stream[at:at+width];at+=width
        gray=sum(int(b)<<i for i,b in enumerate(bits))
        binary=gray
        for shift in (1,2,4):binary^=binary>>shift
        out.append(((binary<<(profile.sf-width))+1)%profile.bins)
    return out


def observation(data,t,profile):
    return dict(time_s=t,channel_hz=2440400000,polarity=1,payload_bins=encode(data,profile))


def waveform(data,profile,polarity=1):
    up=chirp_reference(profile)
    def symbol(value):return np.roll(up,-2*value)
    pieces=[np.tile(up,profile.preamble),symbol(8),symbol(16),up.conj(),up.conj(),up.conj()[:profile.n//4]]
    if profile.sf<=6:pieces += [up,up]
    pieces += [symbol(v) for v in encode(data,profile)]
    return np.concatenate(pieces)**polarity


class ProfileTests(unittest.TestCase):
    def test_firmware_geometry_and_pairing(self):
        counts={'50Hz':18,'100Hz Full':32,'150Hz':21,'250Hz':22,'333Hz Full':42,'500Hz':20}
        periods={'50Hz':20000,'100Hz Full':10000,'150Hz':6666,'250Hz':4000,'333Hz Full':3003,'500Hz':2000}
        for p in PROFILES.values():
            self.assertEqual(len(p.symbol_widths),counts[p.name])
            self.assertEqual(p.interval_us,periods[p.name])
            self.assertLess(p.airtime,p.interval)
            for mode in p.switch_modes:validate_config(config(p,mode))
            wrong='Hybrid' if p.full_resolution else '16ch'
            with self.assertRaises(ValueError):validate_config(config(p,wrong))

    def test_all_profiles_fec_roundtrip_both_polarities(self):
        rng=np.random.default_rng(43)
        for p in PROFILES.values():
            for _ in range(20):
                data=rng.integers(0,256,p.packet_bytes,dtype=np.uint8).tobytes()
                bins=encode(data,p)
                self.assertEqual(decode_long_interleaver(bins,1,p),(data,0))
                self.assertEqual(decode_long_interleaver([(-v)%p.bins for v in bins],-1,p),(data,0))

    def test_500hz_detects_but_does_not_claim_error_correction(self):
        p=get_profile('500');data=bytes(range(8))
        with self.assertRaises(ValueError):decode_long_interleaver(encode(data,p,flip=[0]),1,p)

    def test_synthetic_waveforms_all_profiles(self):
        for p in PROFILES.values():
            cfg=config(p,p.switch_modes[0]);data=sync_packet(p,cfg)
            for pol in (1,-1):
                with self.subTest(rate=p.name,polarity=pol):
                    y=np.r_[np.zeros(256),waveform(data,p,pol),np.zeros(p.n*2)]
                    y*=np.exp(-2j*np.pi*2500*np.arange(len(y))/RATE)
                    recovered=recover_payload(y,257,pol,p)
                    self.assertEqual(decode_long_interleaver(recovered['payload_bins'],pol,p)[0],data)
                    self.assertEqual(recovered['sync_bins'],[(pol*b)%p.bins for b in (8,16)])

    def test_synthetic_iq_crosses_core_boundary_all_profiles(self):
        fs=10_000_000;total=2_000_000
        for p in PROFILES.values():
            with self.subTest(rate=p.name):
                cfg=config(p,p.switch_modes[0]);data=sync_packet(p,cfg)
                base=np.r_[np.zeros(256),waveform(data,p),np.zeros(p.n*2)]
                x=resample_poly(base,80,13)
                samples=np.zeros(total,dtype=np.complex64)
                samples[984000:984000+len(x)]=.3*x
                samples*=np.exp(2j*np.pi*400000*np.arange(total)/fs)
                raw=np.empty(total*2,dtype=np.int8)
                raw[::2]=np.rint(samples.real*128);raw[1::2]=np.rint(samples.imag*128)
                raw+=np.random.default_rng(2).integers(-1,2,len(raw),dtype=np.int8)
                guard=max(100000,math.ceil((p.airtime+.001)*fs/400)*400)
                out=[]
                for core in (0,1000000):
                    lo=max(0,core-guard);hi=min(total,core+1000000+guard)
                    out.extend(extract_window(raw[lo*2:hi*2],lo,core,core+1000000,[400000],p))
                self.assertEqual(len(out),1)
                self.assertEqual(decode_long_interleaver(out[0]['payload_bins'],1,p)[0],data)

    def test_full_crc_and_timing_for_every_rate_and_mode(self):
        for p in PROFILES.values():
            for mode in p.switch_modes:
                with self.subTest(rate=p.name,mode=mode):
                    cfg=config(p,mode);decoder=LiveDecoder(cfg)
                    sync=sync_packet(p,cfg,1)
                    frames=decoder.feed(observation(sync,p.interval,p))
                    self.assertEqual(len(frames),1)
                    frames=decoder.feed(observation(rc_packet(p,cfg,2),2*p.interval,p))
                    self.assertEqual(len(frames),1)
                    self.assertTrue(frames[0]['crc_ok'])
                    self.assertEqual(frames[0]['inferred_nonce'],2)
                    corrupt=bytearray(rc_packet(p,cfg,3));corrupt[3]^=1
                    self.assertFalse(validate_crc(corrupt,decoder.initializer,3))

    def test_normal_and_mavlink_sync_modes_and_reserved_rejection(self):
        # Regression: otaProtocol=0 is normal RC, not an unsupported OTA version.
        # Earlier synthetic fixtures all set 1, masking the real 50Hz failure.
        for p in PROFILES.values():
            mode=p.switch_modes[0];cfg=config(p,mode)
            for protocol in range(4):
                d=LiveDecoder(cfg)
                data=bytearray(sync_packet(p,cfg,1))
                data[0]&=3
                data[4]=(data[4]&~0x60)|(protocol<<5)
                frames=d.feed(observation(seal(data,cfg),p.interval,p))
                self.assertEqual(len(frames),1 if protocol in (0,1) else 0)
                if frames:
                    self.assertEqual(frames[0]['sync']['ota_protocol'],protocol)
                    self.assertEqual(frames[0]['sync']['link_mode'],'normal' if protocol==0 else 'MAVLink')
                else:
                    self.assertEqual(d.counts['unsupported_sync'],1)

    def test_slow_profile_sync_timeout_accommodates_hop_cycle(self):
        p=get_profile('50');d=LiveDecoder(config(p))
        d.feed(observation(sync_packet(p,config(p)),.02,p))
        d.advance(3.3)
        self.assertIsNotNone(d.anchor)
        d.advance(6)
        self.assertIsNone(d.anchor)


class SwitchTests(unittest.TestCase):
    def test_wide_aux_index_and_power_slot(self):
        raw=bytes([0])+pack_four([0,1023,512,100])+bytes([0x80|63,0])
        for nonce in range(256):
            rc=decode_rc(raw,'Wide',nonce)
            idx=((nonce&7)+((nonce>>3)&1))%8
            self.assertEqual(len(rc['channel_updates']),5 if idx==7 else 6)
            if idx==7:
                self.assertNotIn('auxiliary_channel',rc)
                self.assertEqual(rc['uplink_power_code'],63)
            else:
                self.assertEqual(rc['auxiliary_channel'],idx+2)
                self.assertEqual(rc['auxiliary_value_bits'],6)
                self.assertEqual(rc['auxiliary_value'],63)
        with self.assertRaises(ValueError):decode_rc(raw,'Wide')

    def test_full_channel_groups(self):
        p=get_profile('333')
        for mode in ('8ch','12ch','16ch'):
            cfg=config(p,mode)
            low=decode_rc(rc_packet(p,cfg),mode)
            self.assertEqual([u['channel'] for u in low['channel_updates']],list(range(1,9)))
            self.assertEqual(low['channel_updates'][4]['bits'],10) # CH5 is not the armed flag.
            if mode!='8ch':
                high=decode_rc(rc_packet(p,cfg,high=True),mode)
                expected=list(range(9,17)) if mode=='16ch' else list(range(1,5))+list(range(9,13))
                self.assertEqual([u['channel'] for u in high['channel_updates']],expected)
            else:
                with self.assertRaises(ValueError):decode_rc(rc_packet(p,cfg,high=True),mode)

    def test_model_match_off_and_other_ids(self):
        for enabled in (False,True):
            for model_id in (0,1,11,63):
                cfg=config(model_match=enabled,model_id=model_id)
                d=LiveDecoder(cfg)
                self.assertEqual(len(d.feed(observation(sync_packet(DEFAULT,cfg),.004,DEFAULT))),1)
                wrong={**cfg,'model_match':not enabled}
                # ID63 has zero XOR mask; on and off are intentionally equivalent.
                if model_id!=63:
                    self.assertEqual(LiveDecoder(wrong).feed(observation(sync_packet(DEFAULT,cfg),.004,DEFAULT)),[])

    def test_full_model_preserves_groups_and_scrolls(self):
        p=get_profile('333');cfg=config(p,'16ch')
        state=MonitorState(source_label='TEST',model_id=11,channel_count=9,
                           packet_rate=p.name,switch_mode='16ch')
        for t,high in ((0,False),(1.5,True)):
            state.on_frame(dict(time_s=t,channel_hz=2440400000,rc=decode_rc(rc_packet(p,cfg,high=high),'16ch')))
        status=state.snapshot()['status'];status.update(source_seconds=1.5,locked=True)
        state.on_status(status);view=state.snapshot()
        self.assertEqual(len(view['channels']),16)
        self.assertEqual(view['channels'][4]['kind'],'analog')
        self.assertEqual(view['channels'][0]['freshness'],'STALE')
        self.assertEqual(view['channels'][8]['freshness'],'fresh')
        lines=render_lines(view,100,24,scroll=3)
        self.assertIn(' 16  AUX12','\n'.join(l.text for l in lines))
        self.assertIn('333Hz Full / 16ch',lines[1].text)


class ConfigurationTests(unittest.TestCase):
    def test_cli_defaults_and_explicit_settings(self):
        cfg=link_settings(parse_args(['--bind-phrase',PHRASE]))
        self.assertEqual(cfg,dict(binding_phrase=PHRASE,packet_rate='250Hz',
                                  switch_mode='Hybrid',model_match=False,model_id=0,ota_version=4))
        args=parse_args(['--bind-phrase',PHRASE,'--model-match','off',
                         '--model-id','9','--rate','333','--switch-mode','16ch'])
        cfg=link_settings(args)
        self.assertEqual(cfg['packet_rate'],'333Hz Full')
        self.assertEqual(cfg['switch_mode'],'16ch')
        self.assertEqual(cfg['model_id'],9)
        self.assertFalse(cfg['model_match'])
        self.assertFalse(hasattr(args,'config'))
        self.assertFalse(hasattr(args,'no_config'))

    def test_no_settings_file_access_and_legacy_flags_rejected(self):
        with patch.object(Path,'read_text',side_effect=AssertionError('No settings file reads')), \
             patch.object(Path,'is_file',side_effect=AssertionError('No settings discovery')):
            self.assertEqual(link_settings(parse_args(['--bind-phrase',PHRASE]))['binding_phrase'],PHRASE)
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                parse_args([])
        for flags in (['--config','anything.json'],['--no-config']):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                parse_args(['--bind-phrase',PHRASE,*flags])

    def test_help_documents_defaults_modes_and_identity(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaises(SystemExit) as cm:
            parse_args(['--help'])
        self.assertEqual(cm.exception.code,0)
        help_text=output.getvalue()
        for text in ('default: off','default: 0','default: 250','default: Hybrid',
                     '100, 333Hz Full','8ch, 12ch, or 16ch','experimental',
                     'identity seeds','base UID','Examples:','--bind-phrase'):
            self.assertIn(text,help_text)
        self.assertNotIn('--no-config',help_text)
        self.assertNotIn('--config',help_text)

    def test_bare_model_match_and_no_model_match(self):
        for flags,expected in ((['--model-match'],True),(['--model-match','on'],True),
                               (['--no-model-match'],False),(['--model-match','off'],False)):
            cfg=link_settings(parse_args(['--bind-phrase',PHRASE,*flags]))
            self.assertEqual(cfg['model_match'],expected)

    def test_cli_only_replay_end_to_end_wide_and_full(self):
        for rate,mode in (('150','Wide'),('333','16ch')):
            p=get_profile(rate);cfg=config(p,mode,model_match=False,model_id=7)
            with self.subTest(rate=rate),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);iq=root/'synthetic.iq';log=root/'frames.jsonl'
                x=np.zeros(2_000_000,dtype=np.complex64)
                for t,data in ((.095,sync_packet(p,cfg,1)),
                               (.095+p.interval,rc_packet(p,cfg,2,high=mode=='16ch'))):
                    base=np.r_[np.zeros(256),waveform(data,p),np.zeros(p.n*2)]
                    v=resample_poly(base,80,13)
                    start=round(t*10_000_000)
                    x[start:start+len(v)]=.3*v
                x*=np.exp(2j*np.pi*400000*np.arange(len(x))/10_000_000)
                raw=np.empty(len(x)*2,dtype=np.int8)
                raw[::2]=np.rint(x.real*128);raw[1::2]=np.rint(x.imag*128)
                raw+=np.random.default_rng(5).integers(-1,2,len(raw),dtype=np.int8)
                iq.write_bytes(raw.tobytes())
                stdout=io.StringIO();stderr=io.StringIO()
                with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                    code=main(['--bind-phrase',PHRASE,'--model-match','off',
                               '--model-id','7','--rate',rate,'--switch-mode',mode,
                               '--replay',str(iq),'--offset','400000','--format','json','--jsonl',str(log)])
                self.assertEqual(code,0,stderr.getvalue())
                records=[json.loads(line) for line in stdout.getvalue().splitlines()]
                self.assertEqual(len(records),2)
                self.assertTrue(all(r['crc_ok'] for r in records))
                self.assertIn(f"Binding phrase {PHRASE!r} -> base UID",stderr.getvalue())
                self.assertIn(uid_from_phrase(PHRASE).hex(':'),stderr.getvalue())
                channels=[u['channel'] for u in records[1]['rc']['channel_updates']]
                if mode=='16ch':self.assertEqual(channels,list(range(9,17)))
                else:self.assertEqual(records[1]['rc']['auxiliary_value_bits'],6)

    def test_incompatible_mode_and_non_lora_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            parse_args(['--bind-phrase',PHRASE,'--rate','333'])
        for argv in (['--rate','D250'],['--rate','F1000'],['--rate','1000'],
                     ['--rate','250Hz Full'],['--model-id','64']):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):parse_args(['--bind-phrase',PHRASE,*argv])


if __name__=='__main__':unittest.main()
