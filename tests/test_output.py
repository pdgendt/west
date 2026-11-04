# Copyright (c) 2026, Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

# Tests for west._output: per-thread capture of west's standard streams.

import io
import sys
import threading
from unittest.mock import patch

import pytest

from west import _output
from west.commands import Verbosity, WestCommand

COL_RED = "\x1b[91m"
COL_YELLOW = "\x1b[93m"
COL_OFF = "\x1b[0m"


class WestCommandImpl(WestCommand):
    def do_add_parser(self):
        pass

    def do_run(self):
        pass


@pytest.fixture
def cmd():
    cmd = WestCommandImpl(name="x", help="y", description="z")
    cmd.verbosity = Verbosity.DBG_EXTREME
    return cmd


def test_no_sink_by_default():
    assert _output.current_sink() is None
    assert _output.stdout() is sys.stdout
    assert _output.stderr() is sys.stderr


def test_capture_installs_and_removes_sink():
    with _output.capture() as sink:
        assert _output.current_sink() is sink
        assert _output.stdout() is not sys.stdout
        assert _output.stderr() is not sys.stderr
    assert _output.current_sink() is None


def test_sink_records_in_order_and_replays(capsys):
    with _output.capture() as sink:
        print('out 1', file=_output.stdout())
        print('err 1', file=_output.stderr())
        print('out 2', end='', file=_output.stdout(), flush=True)
        sink.write_bytes(_output.STDOUT, b' raw\n')
        sink.write_bytes(_output.STDERR, 'caf\u00e9\n'.encode())
        sink.write_bytes(_output.STDERR, None)
        sink.write_bytes(_output.STDERR, b'')

    # Nothing reaches the real streams while capturing.
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ''

    assert sink.getvalue(_output.STDOUT) == 'out 1\nout 2 raw\n'
    assert sink.getvalue(_output.STDERR) == 'err 1\ncaf\u00e9\n'

    sink.replay()
    captured = capsys.readouterr()
    assert captured.out == 'out 1\nout 2 raw\n'
    assert captured.err == 'err 1\ncaf\u00e9\n'


def test_replay_bytes():
    # Subprocess output is replayed byte for byte when the stream has a
    # binary buffer, and decoded leniently when it doesn't.
    sink = _output.Sink()
    sink.write(_output.STDOUT, 'text ')
    sink.write_bytes(_output.STDOUT, b'raw \xff latin1 \xe9\n')

    with_buffer = io.TextIOWrapper(io.BytesIO(), encoding='ascii', errors='strict')
    without_buffer = io.StringIO()
    for stream in (with_buffer, without_buffer):
        with patch('sys.stdout', stream):
            sink.replay()
    assert with_buffer.buffer.getvalue() == b'text raw \xff latin1 \xe9\n'
    assert without_buffer.getvalue() == 'text raw \\xff latin1 \\xe9\n'


def test_empty_sink():
    sink = _output.Sink()
    sink.replay()  # must not fail


def test_replay_preserves_interleaving():
    # Replay must emit chunks in recorded order even across streams,
    # so verify with a stream pair that shares one buffer.
    log = []

    class Stream:
        def __init__(self, name):
            self.name = name

        def write(self, text):
            log.append((self.name, text))

        def flush(self):
            log.append((self.name, 'flush'))

    sink = _output.Sink()
    sink.write(_output.STDOUT, 'a')
    sink.write(_output.STDERR, 'b')
    sink.write(_output.STDERR, 'c')
    sink.write(_output.STDOUT, 'd')

    out, err = Stream('out'), Stream('err')
    sys_stdout, sys_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        sink.replay()
    finally:
        sys.stdout, sys.stderr = sys_stdout, sys_stderr

    assert log == [
        ('out', 'a'),
        ('out', 'flush'),
        ('err', 'b'),
        ('err', 'c'),
        ('err', 'flush'),
        ('out', 'd'),
        ('out', 'flush'),
    ]


def test_west_command_logging_is_captured(capsys, cmd):
    with _output.capture() as sink:
        cmd.dbg('d')
        cmd.inf('i')
        cmd.banner('b')
        cmd.small_banner('s')
        cmd.wrn('w')
        cmd.err('e')

    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ''

    assert sink.getvalue(_output.STDOUT) == 'DEBUG: d\ni\n\x1b[92m=== b\n\x1b[0m--- s\n'
    assert sink.getvalue(_output.STDERR) == (
        f'{COL_YELLOW}WARNING: w\n{COL_OFF}{COL_RED}ERROR: e\n{COL_OFF}'
    )

    # Replaying gives exactly what the uncaptured calls would have printed.
    sink.replay()
    captured = capsys.readouterr()
    assert captured.out == 'DEBUG: d\ni\n\x1b[92m=== b\n\x1b[0m--- s\n'
    assert captured.err == f'{COL_YELLOW}WARNING: w\n{COL_OFF}{COL_RED}ERROR: e\n{COL_OFF}'


def test_die_output_survives_capture(capsys, cmd):
    cmd.verbosity = Verbosity.INF
    with _output.capture() as sink, pytest.raises(SystemExit) as exit_info:
        cmd.die('boom')
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == ''

    sink.replay()
    assert capsys.readouterr().err == f'{COL_RED}FATAL ERROR: boom\n{COL_OFF}'


def test_sink_is_per_thread(capsys, cmd):
    # A sink installed in one thread must not capture another thread's
    # output, and each worker thread gets its own sink.
    results = {}
    barrier = threading.Barrier(2)

    def worker(name):
        with _output.capture() as sink:
            barrier.wait()
            cmd.inf(f'from {name}')
            barrier.wait()
            results[name] = sink.getvalue(_output.STDOUT)

    threads = [threading.Thread(target=worker, args=(n,)) for n in ('a', 'b')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {'a': 'from a\n', 'b': 'from b\n'}
    assert _output.current_sink() is None

    # The main thread was never captured.
    cmd.inf('main')
    assert capsys.readouterr().out == 'main\n'
