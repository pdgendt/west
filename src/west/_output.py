# Copyright (c) 2026, Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

'''Per-thread capture of west's standard output and error streams.

This is a private module. It lets code that normally prints straight to
``sys.stdout`` / ``sys.stderr`` (west's own log helpers and the output of
git subprocesses) be captured into an in-memory :class:`Sink` instead, so
that several projects can be updated concurrently without their output
interleaving on the terminal.

A sink is installed per *thread* with :func:`capture`. Code that wants to
honour an active sink asks :func:`stdout` / :func:`stderr` for the stream
to print to, which is the real stream when no sink is active. The owner
of the sink later calls :meth:`Sink.replay` from the main thread to write
the recorded output, in order, to the real streams.
'''

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

STDOUT = 1
STDERR = 2

_local = threading.local()


class Sink:
    '''Ordered record of output destined for stdout and stderr.

    West's own messages are recorded as text; what subprocesses print
    is recorded as the bytes they wrote, so that replaying it is
    byte-exact whenever the destination stream exposes its binary
    buffer, whatever the output's encoding.'''

    def __init__(self) -> None:
        self._chunks: list[tuple[int, str | bytes]] = []

    def write(self, fd: int, text: str) -> None:
        '''Record *text* for stream *fd* (``STDOUT`` or ``STDERR``).'''
        if text:
            self._chunks.append((fd, text))

    def write_bytes(self, fd: int, data: bytes | None) -> None:
        '''Record raw subprocess output for stream *fd*.'''
        if data:
            self._chunks.append((fd, data))

    def getvalue(self, fd: int) -> str:
        '''Return everything recorded for stream *fd* as one string.'''
        return ''.join(_as_text(chunk) for chunk_fd, chunk in self._chunks if chunk_fd == fd)

    def replay(self) -> None:
        '''Write the recorded output to the current ``sys.stdout`` and
        ``sys.stderr``, preserving the order in which it was recorded.

        The streams are looked up at call time on purpose: both colorama
        and ``contextlib.redirect_stdout`` work by rebinding them.

        Bytes are written to the stream's ``buffer`` when it has one,
        so they bypass the text layer (including colorama's conversion
        of escape sequences on Windows consoles); otherwise they are
        decoded as UTF-8 and unencodable input is escaped.'''
        last: TextIO | None = None
        for fd, chunk in self._chunks:
            stream = sys.stdout if fd == STDOUT else sys.stderr
            if last is not None and stream is not last:
                last.flush()
            if isinstance(chunk, bytes):
                buffer = getattr(stream, 'buffer', None)
                if buffer is not None:
                    stream.flush()
                    buffer.write(chunk)
                    buffer.flush()
                else:
                    stream.write(_as_text(chunk))
            else:
                stream.write(chunk)
            last = stream
        if last is not None:
            last.flush()


def _as_text(chunk: str | bytes) -> str:
    if isinstance(chunk, bytes):
        return chunk.decode('utf-8', errors='backslashreplace')
    return chunk


class _Writer:
    # Minimal text-stream stand-in that forwards to the thread's sink.
    # Only what print() needs: write() and flush().

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def write(self, text: str) -> int:
        sink = current_sink()
        assert sink is not None
        sink.write(self._fd, text)
        return len(text)

    def flush(self) -> None:
        pass


_STDOUT_WRITER = _Writer(STDOUT)
_STDERR_WRITER = _Writer(STDERR)


def current_sink() -> Sink | None:
    '''Return the sink installed for the calling thread, if any.'''
    return getattr(_local, 'sink', None)


def stdout() -> TextIO:
    '''Return the stream west should print standard output to right now.'''
    return _STDOUT_WRITER if current_sink() is not None else sys.stdout  # type: ignore[return-value]


def stderr() -> TextIO:
    '''Return the stream west should print standard error to right now.'''
    return _STDERR_WRITER if current_sink() is not None else sys.stderr  # type: ignore[return-value]


@contextmanager
def capture() -> Iterator[Sink]:
    '''Install a fresh :class:`Sink` for the calling thread.

    Nesting is not supported: the previous sink (if any) is restored on
    exit but output is not forwarded to it.'''
    previous = current_sink()
    sink = Sink()
    _local.sink = sink
    try:
        yield sink
    finally:
        _local.sink = previous
