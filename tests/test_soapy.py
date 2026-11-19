"""Focused optional-transport contracts; not physical RF validation."""
import ctypes as C
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

import numpy as np

from elrs_monitor.cli import parse_args
from elrs_monitor.soapy import SoapyAPI, SoapySource, OVERFLOW, HAS_TIME, END_ABRUPT


class FakeAPI:
    def __init__(self, reads=(), formats=('CS8',), fail=None, rate=10_000_000):
        self.reads = iter(reads)
        self.formats, self.fail, self.rate = formats, fail, rate
        self.calls = []
        self.gains, self.bias = {}, 'true'

    def get(self, name, *args):
        self.calls.append((name,args))
        if name == self.fail:
            raise RuntimeError('injected '+name)
        if name == 'makeStrArgs': return 123
        if name == 'getSampleRate': return self.rate
        if name == 'hasGainMode': return True
        if name == 'getGainElement': return self.gains[args[-1]]
        if name == 'setupStream': return 456
        if name == 'getStreamMTU': return 32768
        if name == 'readStream':
            data, flags, timestamp = next(self.reads)
            args[4]._obj.value, args[5]._obj.value = flags, timestamp
            if isinstance(data, int): return data
            C.memmove(args[2][0], data, len(data))
            return len(data)//(2 if self.formats[0]=='CS8' else 8)
        return 0

    def call(self, name, *args):
        self.get(name, *args)
        if name == 'setGainElement': self.gains[args[-2]] = args[-1]
        if name == 'writeSetting': self.bias = args[-1].decode()

    def string(self, name, *args):
        return 'HackRF' if name=='getDriverKey' else self.bias

    def strings(self, name, *args):
        return self.formats if name=='getStreamFormats' else ['AMP','LNA','VGA']

    def set_log_handler(self, handler):
        self.log_handler = handler
        self.calls.append(('log_handler',(handler,)))

    def frequency_ranges(self, device): return [(0,6_000_000_000)]

    def settings(self, device): return ['bias_tx']


class SoapyTests(unittest.TestCase):
    def args(self, *extra):
        return parse_args(['--uid','13ad5fd3','--backend','soapy',*extra])

    def test_native_log_callback_routes_events_without_terminal_writes(self):
        api = SoapyAPI.__new__(SoapyAPI)
        register = Mock()
        api.lib = SimpleNamespace(SoapySDR_registerLogHandler=register)
        messages = []
        api.set_log_handler(lambda text,level: messages.append((text,level)))
        callback = register.call_args.args[0]
        with patch('sys.stdout') as out,patch('sys.stderr') as err:
            callback(6,b'Opening HackRF\n')
            callback(9,b'O')
            callback(3,b'failure')
            self.assertEqual(messages,[('SoapySDR: Opening HackRF','info'),
                                       ('SoapySDR: O','warning'),('SoapySDR: failure','error')])
            api.set_log_handler(lambda *a: (_ for _ in ()).throw(ValueError('observer failed')))
            api._log_callback(6,b'never escape into ctypes/stderr')
            out.write.assert_not_called();err.write.assert_not_called()
        api.set_log_handler(None)
        self.assertFalse(register.call_args.args[0])
        self.assertIsNone(api._log_callback)
        # Partial startup failures also restore the default logger, exactly once.
        fake = FakeAPI(fail='makeStrArgs')
        source = SoapySource(self.args(),fake,on_message=lambda *a:None)
        with self.assertRaises(RuntimeError):source.start()
        source.close()
        handlers = [a[0] for n,a in fake.calls if n=='log_handler']
        self.assertEqual(len(handlers),2)
        self.assertIsNone(handlers[-1])

    def test_rx_only_configuration_duration_and_cleanup(self):
        api = FakeAPI([(b'\x01\xff'*20000,0,0)])
        s = SoapySource(self.args('--seconds','.001','--serial','1234'), api)
        try:
            s.start(); s.thread.join(timeout=1)
            self.assertIsNone(s.error)
            b = s.queue.get_nowait()
            self.assertEqual(b.start,0)
            self.assertEqual(b.data,b'\x01\xff'*10000)
            self.assertTrue(s.finished.is_set())
            self.assertEqual(api.gains,{b'LNA':16,b'VGA':16,b'AMP':0})
            self.assertEqual(api.bias,'false')
        finally:
            s.close(); s.close()
        names = [n for n,a in api.calls]
        self.assertEqual(names.count('deactivateStream'),1)
        self.assertEqual(names.count('closeStream'),1)
        self.assertEqual(names.count('unmake'),1)
        setup = next(a for n,a in api.calls if n=='setupStream')
        self.assertEqual(setup[1],1)  # SOAPY_SDR_RX, never TX.
        self.assertFalse(any('writeStream' in n for n in names))
        self.assertEqual(api.calls[0][1],(b'driver=hackrf,serial=1234',))

    def test_cf32_conversion_and_unknown_discontinuities_fail_closed(self):
        values = np.tile(np.array([-2,-1,0,.5,1,2],dtype=np.float32),4000)
        api = FakeAPI([(values.tobytes(),0,0)],formats=('CF32',))
        s = SoapySource(self.args('--seconds','.001'),api)
        try:
            s.start();s.thread.join(timeout=1)
            self.assertIsNone(s.error)
            actual = np.frombuffer(s.queue.get_nowait().data,dtype=np.int8)
            np.testing.assert_array_equal(actual[:6],[-128,-127,0,64,127,127])
        finally:s.close()
        # Integer continuity arithmetic must also work with large UTC-like ns.
        base = 8_000_000_000_000_000_000
        api = FakeAPI([(bytes(10000),HAS_TIME,base),(bytes(10000),HAS_TIME,base+500_000)])
        s = SoapySource(self.args('--seconds','.001'),api)
        try:
            s.start();s.thread.join(timeout=1)
            self.assertIsNone(s.error)
            self.assertEqual(s.samples,10000)
        finally:s.close()
        for reads in (
            [(OVERFLOW,0,0)],
            [(0,END_ABRUPT,0)],
            [(b'\x00\x00'*100,END_ABRUPT,0)],
            [(b'\x00\x00'*100,HAS_TIME,0),(b'\x00\x00'*100,HAS_TIME,1_000_000)]):
            api = FakeAPI(reads);s = SoapySource(self.args(),api)
            try:
                s.start();s.thread.join(timeout=1)
                self.assertIsInstance(s.error,RuntimeError)
                self.assertTrue(s.finished.is_set())
                self.assertLessEqual(s.samples,100)
            finally:s.close()

    def test_retune_epoch_gap_bounded_queue_and_recalibration(self):
        s = SoapySource(self.args(),FakeAPI())
        with patch('elrs_monitor.soapy.time.monotonic',return_value=1):
            s._receive(bytes(200000))
        with patch('elrs_monitor.soapy.time.monotonic',side_effect=[1.002,1.003]):
            s.tune(2410000000)
        with patch('elrs_monitor.soapy.time.monotonic',return_value=1.01):
            s._receive(bytes(200000))
        blocks = [s.queue.get_nowait() for _ in range(3)]
        self.assertEqual([b.epoch for b in blocks],[0,0,1])
        self.assertEqual(blocks[-1].center_hz,2410000000)
        self.assertLessEqual(abs(blocks[1].end-120000),1)
        self.assertLessEqual(abs(blocks[2].start-170000),1)
        self.assertLessEqual(abs(s.retune_discarded_samples-50000),2)
        samples = s.samples;s.recalibrate()
        self.assertEqual(s.samples,samples)
        self.assertEqual(s.epochs[0].start,samples+40000)
        for _ in range(20):s._enqueue(blocks[0])
        self.assertEqual(s.queue.qsize(),6)
        self.assertEqual(s.dropped_blocks,14)

    def test_startup_failure_cleanup_and_backend_cli_boundaries(self):
        for fail, rate in (('setFrequency',10_000_000),('activateStream',10_000_000),(None,2_400_000)):
            api = FakeAPI(fail=fail,rate=rate)
            s = SoapySource(self.args(),api)
            with self.assertRaises((ValueError,RuntimeError)):s.start()
            s.close()
            self.assertEqual([n for n,a in api.calls].count('unmake'),1)
            if fail == 'activateStream':
                self.assertEqual([n for n,a in api.calls].count('closeStream'),1)
            else:
                self.assertFalse(any(n=='activateStream' for n,a in api.calls))
        api = FakeAPI();api.frequency_ranges = lambda _: [(500_000,1_766_000_000)]
        with self.assertRaisesRegex(ValueError,'2.4GHz'):
            SoapySource(self.args(),api).start()
        self.assertEqual(parse_args(['--uid','13ad5fd3']).backend,'native')
        for extra in (['--gain','AMP=14'],['--gain','LNA=nan'],['--gain','LNA=4','--gain','LNA=8'],
                      ['--replay','dummy.iq']):
            with self.subTest(extra=extra),patch('sys.stderr'),self.assertRaises(SystemExit):
                self.args(*extra)
        with patch('sys.stderr'),self.assertRaises(SystemExit):
            parse_args(['--uid','13ad5fd3','--device','driver=hackrf'])
