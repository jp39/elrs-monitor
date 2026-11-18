import contextlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from elrs_monitor.cli import parse_args, main
from elrs_monitor.engine import run_receiver
from test_live import CONFIG, observation, rc_packet, sync_packet


class Observer:
    def __init__(self):
        self.frames=[]
        self.statuses=[]
    def on_frame(self,frame):
        self.frames.append(frame)
    def on_status(self,status):
        self.statuses.append(status)
    def on_message(self,message,level='info'):
        pass


class EngineTests(unittest.TestCase):
    def test_frame_log_and_final_status(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            iq=p/'capture.iq'
            with iq.open('wb') as f:
                f.truncate(4_000_000)
            log=p/'frames.jsonl'
            args=parse_args(['--bind-phrase',CONFIG['binding_phrase'],'--replay',str(iq),'--jsonl',str(log)])
            observer=Observer()
            def synthetic(window,offsets,profile):
                return [observation(sync_packet(1),.004),observation(rc_packet(2),.008)] if window.core_start==0 else []
            with patch('elrs_monitor.engine.process_window',side_effect=synthetic):
                run_receiver(args,CONFIG,observer)
            self.assertEqual(len(observer.frames),2)
            self.assertEqual(len(log.read_text().splitlines()),2)
            self.assertEqual(log.stat().st_mode & 0o777,0o600)
            self.assertTrue(observer.statuses[-1]['done'])
            self.assertEqual(observer.statuses[-1]['source_seconds'],.2)
            self.assertEqual(observer.statuses[-1]['counters']['rc'],1)

    def test_existing_log_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            log=p/'frames.jsonl'; log.write_text('preserve')
            args=parse_args(['--bind-phrase',CONFIG['binding_phrase'],'--replay',str(p/'missing.iq'),'--jsonl',str(log)])
            with patch('elrs_monitor.engine.Source.start') as start:
                with self.assertRaises(FileExistsError):
                    run_receiver(args,CONFIG,Observer())
                start.assert_not_called()
            self.assertEqual(log.read_text(),'preserve')

    def test_cancel_before_start_never_opens_hardware(self):
        args=parse_args(['--bind-phrase',CONFIG['binding_phrase'],])
        stop=threading.Event(); stop.set()
        with patch('elrs_monitor.engine.Source.start') as start:
            run_receiver(args,CONFIG,Observer(),stop)
            start.assert_not_called()

    def test_error_has_final_status(self):
        args=parse_args(['--bind-phrase',CONFIG['binding_phrase'],'--replay','/nonexistent/elrs-monitor-test.iq'])
        observer=Observer()
        with self.assertRaises(FileNotFoundError):
            run_receiver(args,CONFIG,observer)
        self.assertTrue(observer.statuses[-1]['done'])

    def test_identity_required_and_empty_phrase_rejected(self):
        for argv in ([],['--bind-phrase','']):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as cm:
                main(argv)
            self.assertEqual(cm.exception.code,2)

    def test_identity_printed_to_stderr_without_polluting_json_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            iq=Path(directory)/'blank.iq';iq.write_bytes(b'\0'*200_000)
            stdout=io.StringIO();stderr=io.StringIO()
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                code=main(['--bind-phrase','julian','--model-match','on','--model-id','0',
                           '--replay',str(iq),'--format','json'])
            self.assertEqual(code,0)
            self.assertEqual(stdout.getvalue(),'')
            self.assertIn("Binding phrase 'julian' -> base UID 16:b3:13:ad:5f:d3",stderr.getvalue())
            self.assertIn('Link UID 13:ad:5f:d3 | sync UID bytes 5f:ec',stderr.getvalue())
            self.assertIn('"uid": "16:b3:13:ad:5f:d3"',stderr.getvalue())

    def test_invalid_cli_options(self):
        for argv in (['--workers','0'],['--pace'],['--offset','0'],
                     ['--stale-after','nan'],['--seconds','-1'],
                     ['--tui','--format','json']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args(['--bind-phrase',CONFIG['binding_phrase'],*argv])


if __name__ == '__main__':
    unittest.main()
