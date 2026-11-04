# Copyright (c) 2026, Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

# Unit tests for west.app._parallel, using fake "projects" and no git.

import _thread
import os
import subprocess
import sys
import threading
import time

import pytest

from west import _output
from west.app import _parallel
from west.manifest import Project


def project(name, path=None):
    return Project(name, 'url', path=path or name)


def names(projects):
    return [p.name for p in projects]


def test_chains_independent_projects():
    ps = [project('a'), project('b', 'sub/b'), project('c')]
    assert [names(c) for c in _parallel.chains(ps)] == [['a'], ['b'], ['c']]


def test_chains_nested_paths():
    # 'a/b' and 'a/b/c' live inside 'a', so the three must run in one
    # chain, in the given order; 'ab' only looks similar.
    ps = [
        project('x'),
        project('a'),
        project('ab'),
        project('a/b/c'),
        project('y'),
        project('a/b'),
    ]
    assert [names(c) for c in _parallel.chains(ps)] == [
        ['x'],
        ['a', 'a/b/c', 'a/b'],
        ['ab'],
        ['y'],
    ]


def test_chains_nested_before_parent():
    # The outer project appears after the nested one: still one chain,
    # keeping the given (serial) order.
    ps = [project('a/b'), project('a')]
    assert [names(c) for c in _parallel.chains(ps)] == [['a/b', 'a']]


def test_path_key(tmp_path):
    base = tuple(os.path.normcase(part) for part in tmp_path.resolve().parts)
    assert _parallel.path_key(tmp_path) == base
    assert _parallel.path_key(tmp_path / 'x' / 'y') == base + ('x', 'y')
    (tmp_path / 'x').mkdir()
    assert _parallel.path_key(tmp_path / 'x' / 'y') == base + ('x', 'y')
    assert _parallel.path_key(tmp_path / 'x' / '..' / 'x' / 'y') == base + ('x', 'y')


@pytest.mark.skipif(sys.platform == 'win32', reason='symlinks need privileges on Windows')
def test_chains_resolve_aliases(tmp_path):
    # A project reached through a symlink to another project's directory
    # is nested in it, even though the manifest paths don't show it; and
    # two spellings of one directory are the same directory.
    (tmp_path / 'real').mkdir()
    (tmp_path / 'alias').symlink_to(tmp_path / 'real', target_is_directory=True)
    ps = [
        Project('real', 'url', path='real', topdir=tmp_path),
        Project('other', 'url', path='other', topdir=tmp_path),
        Project('inner', 'url', path='alias/inner', topdir=tmp_path),
        Project('same', 'url', path='./real/', topdir=tmp_path),
    ]
    assert [names(c) for c in _parallel.chains(ps)] == [['real', 'inner', 'same'], ['other']]


def test_auto_jobs():
    assert 1 <= _parallel.auto_jobs() <= _parallel.MAX_AUTO_JOBS


class Recorder:
    # fn() stand-in that records the calling thread and which sink was
    # active, and misbehaves on request.

    def __init__(self, fail=(), raise_for=None):
        self.calls = []
        self.fail = fail
        self.raise_for = raise_for
        self.lock = threading.Lock()

    def __call__(self, p):
        with self.lock:
            self.calls.append((p.name, threading.current_thread().name))
        print(f'working on {p.name}', file=_output.stdout())
        if p.name in self.fail:
            raise subprocess.CalledProcessError(1, ['git'])
        if self.raise_for == p.name:
            raise RuntimeError(f'{p.name} exploded')


@pytest.mark.parametrize('jobs', [1, 2, 4])
def test_run_for_projects(capsys, jobs):
    ps = [project(n) for n in 'abcdef']
    fn = Recorder(fail={'b', 'e'})

    ok, failed = _parallel.run_for_projects(ps, fn, jobs)

    # Results come back in the input order no matter which finished first.
    assert names(ok) == ['a', 'c', 'd', 'f']
    assert names(failed) == ['b', 'e']
    assert sorted(n for n, _ in fn.calls) == list('abcdef')

    threads = {t for _, t in fn.calls}
    out = capsys.readouterr().out
    if jobs == 1:
        assert threads == {'MainThread'}
        assert out == ''.join(f'working on {n}\n' for n in 'abcdef')
    else:
        assert 'MainThread' not in threads
        assert all(t.startswith('west') for t in threads)
        # Every project's output was replayed exactly once.
        assert sorted(out.splitlines()) == sorted(f'working on {n}' for n in 'abcdef')


def test_run_for_projects_bad_jobs():
    with pytest.raises(ValueError):
        _parallel.run_for_projects([project('a')], lambda p: None, 0)


def test_run_for_projects_exception_aborts(capsys):
    # An exception other than CalledProcessError is re-raised in the
    # calling thread, after the output of what did run was printed.
    ps = [project(n) for n in 'abc']
    fn = Recorder(raise_for='b')

    with pytest.raises(RuntimeError, match='b exploded'):
        _parallel.run_for_projects(ps, fn, 2)

    out = capsys.readouterr().out
    assert 'working on b' in out


def test_run_for_projects_systemexit_aborts(capsys):
    # die() ends in sys.exit(); the output written before it must still
    # make it to the terminal.
    def fn(p):
        print(f'fatal in {p.name}', file=_output.stderr())
        raise SystemExit(3)

    with pytest.raises(SystemExit) as exit_info:
        _parallel.run_for_projects([project('a'), project('b')], fn, 2)
    assert exit_info.value.code == 3
    assert 'fatal in ' in capsys.readouterr().err


def test_run_for_projects_unbuffered(capsys):
    # Without buffering nothing is captured: output is live (and may
    # interleave), and the reporter is never given a sink.
    ps = [project(n) for n in 'abc']
    fn = Recorder()

    class NoSinkReporter(_parallel.Reporter):
        def finished(self, sink):
            raise AssertionError('unbuffered runs must not replay sinks')

    ok, failed = _parallel.run_for_projects(ps, fn, 2, NoSinkReporter(), buffered=False)
    assert names(ok) == ['a', 'b', 'c']
    assert not failed
    assert sorted(capsys.readouterr().out.splitlines()) == sorted(f'working on {n}' for n in 'abc')


def test_run_for_projects_error_stops_chains():
    # A fatal error in one chain stops the other chains before their
    # next project, instead of letting them run to the end.
    release = threading.Event()

    def fn(p):
        if p.name == 'x':
            raise RuntimeError('x exploded')
        if p.name == 'a':
            # Keep the chain busy until well after the error has been
            # seen by the main thread (which polls every 0.25 s).
            release.wait(5)

    threading.Timer(1.0, release.set).start()
    ps = [project('a'), project('a/b'), project('x')]
    with pytest.raises(RuntimeError, match='x exploded'):
        _parallel.run_for_projects(ps, fn, 2)
    assert release.is_set()


def test_run_for_projects_interrupt_replays_output(capsys):
    # On Ctrl-C the output of the projects that were in flight is still
    # shown, so the user can see which ones were left half done.
    def fn(p):
        print(f'{p.name} was running', file=_output.stdout())
        if p.name == 'a':
            # Interrupt once the main thread is polling, like a real
            # Ctrl-C would; then take a moment to "fail", like git does
            # after receiving the same Ctrl-C.
            time.sleep(0.4)
            _thread.interrupt_main()
            time.sleep(0.4)

    with pytest.raises(KeyboardInterrupt):
        _parallel.run_for_projects([project('a'), project('b')], fn, 2)
    assert 'a was running' in capsys.readouterr().out


def test_run_for_projects_chain_order():
    # Nested projects run after their parent in the same thread.
    ps = [project('a'), project('a/b'), project('c')]
    fn = Recorder()
    ok, failed = _parallel.run_for_projects(ps, fn, 4)
    assert names(ok) == ['a', 'a/b', 'c']
    assert not failed
    calls = dict(fn.calls)
    assert calls['a'] == calls['a/b']
    order = [n for n, _ in fn.calls]
    assert order.index('a') < order.index('a/b')
