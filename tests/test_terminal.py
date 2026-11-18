"""Exercise real curses in a pseudo-terminal, with no radio required."""
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from test_live import CONFIG
from elrs_monitor.protocol import uid_from_phrase


@unittest.skipUnless(os.name=='posix','curses/PTY integration requires Unix')
class TerminalTests(unittest.TestCase):
    def test_binding_handover_retains_normal_channels_after_eof(self):
        import fcntl
        import pty
        import termios
        from test_binding import write_iq, binding_packet, PROFILE
        from test_modes import PHRASE, config, sync_packet, rc_packet
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);iq=p/'binding.iq';log=p/'binding.jsonl'
            uid=uid_from_phrase(PHRASE)
            cfg=config(PROFILE,'Wide')
            write_iq(iq,[(t,binding_packet(uid)) for t in (.095,.135,.175)]
                     +[(.235,sync_packet(PROFILE,cfg,1)),(.255,rc_packet(PROFILE,cfg,2))])
            master,slave=pty.openpty()
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,100,0,0))
            process=None;output=bytearray()
            def drain_until(needle,timeout=8):
                deadline=time.monotonic()+timeout
                while needle not in output and time.monotonic()<deadline:
                    if select.select([master],[],[],.1)[0]:
                        try:data=os.read(master,65536)
                        except OSError:break
                        if not data:break
                        output.extend(data)
                self.assertIn(needle,output)
            try:
                process=subprocess.Popen(
                    [sys.executable,'-m','elrs_monitor','--discover-binding','--tui',
                     '--rate','50','--switch-mode','Wide','--model-match','on','--model-id','11',
                     '--replay',str(iq),'--jsonl',str(log)],
                    stdin=slave,stdout=slave,stderr=slave,
                    env={**os.environ,'TERM':'xterm-256color'},start_new_session=True)
                drain_until(b'learned UID (00:00 prefix)')
                self.assertIsNone(process.poll())
                os.write(master,b'q')
                process.wait(timeout=8)
                drain_until(('Discovered UID: 00:00:'+uid[2:].hex(':')).encode())
                self.assertEqual(process.returncode,0)
                self.assertNotIn(b'Traceback',output)
                self.assertEqual(len(log.read_text().splitlines()),5)
            finally:
                if process and process.poll() is None:
                    process.kill();process.wait()
                os.close(master);os.close(slave)

    def test_replay_pause_reset_resize_and_quit(self):
        import fcntl
        import pty
        import termios
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            iq=p/'blank.iq'; iq.write_bytes(b'\x00'*200_000)
            log=p/'frames.jsonl'
            master,slave=pty.openpty()
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,100,0,0))
            process=None
            output=bytearray()
            def read_until(needle,timeout=8):
                deadline=time.monotonic()+timeout
                while needle not in output and time.monotonic()<deadline:
                    if select.select([master],[],[],.1)[0]:
                        try:
                            data=os.read(master,65536)
                        except OSError:
                            break
                        if not data:
                            break
                        output.extend(data)
                    if process.poll() is not None:
                        break
                self.assertIn(needle,output)
            try:
                process=subprocess.Popen(
                    [sys.executable,'-m','elrs_monitor','--bind-phrase',CONFIG['binding_phrase'],
                     '--model-match','on','--model-id','11',
                     '--replay',str(iq),'--tui','--jsonl',str(log)],
                    stdin=slave,stdout=slave,stderr=slave,
                    env={**os.environ,'TERM':'xterm-256color'},start_new_session=True)
                read_until(b'ELRS MONITOR')
                read_until(('UID '+uid_from_phrase(CONFIG['binding_phrase']).hex(':')).encode())
                # curses may reuse the existing leading S from SEARCHING.
                read_until(b'TOPPED / EOF')
                os.write(master,b' ')
                read_until(b'DISPLAY PAUSED')
                os.write(master,b'r')
                # Resize below supported dimensions and verify a safe fallback.
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',18,60,0,0))
                process.send_signal(signal.SIGWINCH)
                read_until(b'Terminal too small')
                os.write(master,b'q')
                process.wait(timeout=8)
                self.assertEqual(process.returncode,0)
                self.assertNotIn(b'Traceback',output)
                self.assertTrue(log.exists())
                self.assertEqual(log.stat().st_mode & 0o777,0o600)
            finally:
                if process and process.poll() is None:
                    process.kill();process.wait()
                os.close(master);os.close(slave)


if __name__ == '__main__':
    unittest.main()
