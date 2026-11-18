"""Bounded receive-only HackRF/replay capture and overlapped sample windows."""
from collections import deque
from dataclasses import dataclass
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

from .constants import FS

BLOCK_SAMPLES = FS//10  # 100 ms; processing adds a one-block lookahead.
GUARD_SAMPLES = FS//100
DEFAULT_OFFSETS = tuple(range(-3_600_000, 4_400_001, 1_000_000))


@dataclass
class Block:
    start: int
    data: bytes

    @property
    def end(self):
        return self.start+len(self.data)//2


@dataclass
class Window:
    data: bytes
    start: int
    core_start: int
    core_end: int


class WindowAssembler:
    def __init__(self, guard_samples=GUARD_SAMPLES):
        self.pending = None
        self.left = b''
        self.guard_samples = guard_samples

    def feed(self, block):
        gap = self.pending is not None and block.start != self.pending.end
        window = None
        if self.pending is not None and not gap:
            old = self.pending
            window = Window(self.left+old.data+block.data[:self.guard_samples*2],
                            old.start-len(self.left)//2, old.start, old.end)
            self.left = old.data[-self.guard_samples*2:]
        elif gap:
            # Do not stitch across dropped samples or trust the old nonce grid.
            self.left = b''
        self.pending = block
        return window, gap

    def finish(self):
        if self.pending is None:
            return None
        old = self.pending
        self.pending = None
        return Window(self.left+old.data, old.start-len(self.left)//2,
                      old.start, old.end)


class Source:
    """Drain HackRF independently; bounded queue drops oldest blocks if behind."""
    def __init__(self, args, on_radio_message=None):
        self.args = args
        self.on_radio_message = on_radio_message
        self.queue = queue.Queue(maxsize=6)
        self.stop = threading.Event()
        self.child = None
        self.stream = None
        self.error = None
        self.blocks = 0
        self.samples = 0
        self.dropped_blocks = 0
        self.dropped_samples = 0
        self.radio_messages = deque(maxlen=16)
        self.thread = None
        self.radio_thread = None
        self.last_read = time.monotonic()

    def start(self):
        if self.args.replay:
            self.stream = self.args.replay.open('rb')
        else:
            executable = shutil.which('hackrf_transfer')
            if executable is None:
                raise ValueError('hackrf_transfer not found; install the HackRF utilities')
            # Deliberately no TX options. RF amp and antenna-port bias power off.
            command = [executable, '-r', '-', '-f', '2440000000', '-s', str(FS),
                       '-a', '0', '-p', '0', '-l', str(self.args.lna),
                       '-g', str(self.args.vga)]
            if self.args.serial:
                command += ['-d', self.args.serial]
            if self.args.seconds is not None:
                command += ['-n', str(int(self.args.seconds*FS))]
            self.child = subprocess.Popen(command, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, bufsize=0,
                                          start_new_session=True)
            self.stream = self.child.stdout
            self.radio_thread = threading.Thread(target=self._drain_stderr, daemon=True)
            self.radio_thread.start()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        return self

    def _drain_stderr(self):
        for raw in iter(self.child.stderr.readline, b''):
            message = raw.decode(errors='replace').rstrip()
            self.radio_messages.append(message)
            if self.on_radio_message is not None:
                self.on_radio_message(message)

    def _put(self, item, drop):
        while not self.stop.is_set():
            try:
                self.queue.put(item, timeout=.05 if not drop else 0)
                return
            except queue.Full:
                if drop:
                    try:
                        old = self.queue.get_nowait()
                    except queue.Empty:
                        continue
                    if old is not None:
                        self.dropped_blocks += 1
                        self.dropped_samples += len(old.data)//2

    def _read(self):
        try:
            started = time.monotonic()
            limit = int(self.args.seconds*FS) if self.args.seconds is not None else None
            while not self.stop.is_set():
                count = BLOCK_SAMPLES if limit is None else min(BLOCK_SAMPLES, limit-self.samples)
                if count <= 0:
                    break
                parts = bytearray()
                needed = count*2
                while len(parts) < needed and not self.stop.is_set():
                    part = self.stream.read(needed-len(parts))
                    if not part:
                        break
                    parts.extend(part)
                if not parts:
                    break
                if len(parts) % 2:
                    raise ValueError('Source ended with an incomplete IQ pair')
                block = Block(self.samples, bytes(parts))
                self.blocks += 1
                self.samples = block.end
                self.last_read = time.monotonic()
                if self.args.replay and self.args.pace:
                    if self.stop.wait(max(0, started+self.samples/FS-time.monotonic())):
                        break
                self._put(block, drop=not self.args.replay or self.args.pace)
                if len(parts) < needed:
                    break
        except Exception as exc:
            self.error = exc
        finally:
            self._put(None, drop=False)

    def close(self):
        self.stop.set()
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.radio_thread is not None:
            self.radio_thread.join(timeout=2)
        if self.stream is not None:
            self.stream.close()
        if self.child is not None:
            self.child.stderr.close()

