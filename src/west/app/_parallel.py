# Copyright (c) 2026, Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

'''Run a per-project operation for several projects concurrently.

This is a private helper for the project-iterating commands (currently
``west update``). The work itself is done by git subprocesses, so a
thread pool is all the parallelism that is needed; the Python side is
mostly waiting.

Output is the hard part. With more than one job, each project's output
(west's own messages and whatever git prints) is captured per thread
with :mod:`west._output` and written to the terminal as one block when
that project finishes, so blocks never interleave. A :class:`Reporter`
decides how a finished block is shown.
'''

import concurrent.futures
import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from west import _output
from west.manifest import Project

# Upper bound for the automatic job count. The work is network and disk
# bound rather than CPU bound, so more jobs than this rarely help and
# mostly hammer the git servers.
MAX_AUTO_JOBS = 8


def auto_jobs() -> int:
    '''Number of jobs to use when the user asks for "as many as sensible".'''
    return min(os.cpu_count() or 1, MAX_AUTO_JOBS)


def path_key(path: str | os.PathLike) -> tuple[str, ...]:
    '''Identity of a directory that may not exist yet, as path components.

    The nearest existing ancestor is resolved (symlinks, junctions,
    ``..``) and every component is case-normalized, so two spellings of
    one directory get the same key on a case-insensitive filesystem, and
    ``key_a == key_b[:len(key_a)]`` means *a* contains *b*.'''
    existing = Path(path)
    missing: list[str] = []
    while not existing.exists():
        parent = existing.parent
        if parent == existing:
            break
        missing.append(existing.name)
        existing = parent
    real = Path(os.path.normcase(os.path.realpath(existing)))
    return real.parts + tuple(os.path.normcase(name) for name in reversed(missing))


def chains(projects: Sequence[Project]) -> list[list[Project]]:
    '''Group *projects* so that overlapping project paths stay ordered.

    Most projects are independent and become a chain of their own. A
    project whose directory lies inside (or is) another listed project's
    directory is appended to that outer project's chain instead, so the
    two are updated one after the other in their original order, exactly
    as a serial update would. Returns the chains in first-seen order.'''
    keys = [path_key(p.abspath or p.path) for p in projects]
    result: list[list[Project]] = []
    chain_of: dict[tuple[str, ...], list[Project]] = {}

    for project, key in zip(projects, keys, strict=True):
        root = key
        for other in keys:
            if len(other) < len(root) and key[: len(other)] == other:
                root = other  # keep going: we want the outermost one
        chain = chain_of.get(root)
        if chain is None:
            chain = chain_of[root] = []
            result.append(chain)
        chain.append(project)

    return result


class Reporter:
    '''Shows the output of finished projects; the base class just prints
    each block as it completes (completion order), which is right for
    pipes, files and CI logs.'''

    def finished(self, sink: _output.Sink) -> None:
        sink.replay()


@dataclass
class _Chain:
    '''Projects updated one after the other on one worker thread, plus
    the bookkeeping the main thread needs to follow it.'''

    projects: list[Project]
    future: concurrent.futures.Future | None = None
    # Set by the main thread once the results have been taken over.
    collected: bool = False

    @property
    def finished(self) -> bool:
        return self.future is not None and self.future.done() and not self.future.cancelled()


def run_for_projects(
    projects: Sequence[Project],
    fn: Callable[[Project], None],
    jobs: int,
    reporter: Reporter | None = None,
    buffered: bool = True,
) -> tuple[list[Project], list[Project]]:
    '''Call ``fn(project)`` for every project, using up to *jobs* threads.

    Returns ``(succeeded, failed)``, both in the order of *projects*.
    A ``subprocess.CalledProcessError`` raised by *fn* marks that project
    as failed and the run continues; any other exception (including
    ``SystemExit`` from ``die()``) stops scheduling new work, lets the
    running projects finish so their output is not lost, and is then
    re-raised.

    With ``jobs == 1`` nothing is captured and *fn* runs in the calling
    thread, so output is live, exactly as a plain loop would behave.
    With ``buffered=False`` nothing is captured either: output is live
    and the projects' output may interleave.'''
    if jobs < 1:
        raise ValueError(f'jobs must be positive, not {jobs}')

    succeeded: list[Project] = []
    failed: list[Project] = []

    if jobs == 1 or len(projects) <= 1:
        for project in projects:
            try:
                fn(project)
                succeeded.append(project)
            except subprocess.CalledProcessError:
                failed.append(project)
        return succeeded, failed

    if reporter is None:
        reporter = Reporter()
    order = {id(p): i for i, p in enumerate(projects)}
    work = [_Chain(chain) for chain in chains(projects)]
    # Set once the run is being abandoned (fatal error, Ctrl-C): chains
    # then stop before starting their next project.
    stop = threading.Event()

    def run_chain(chain: _Chain):
        done: list[Project] = []
        bad: list[Project] = []
        exc: BaseException | None = None
        for project in chain.projects:
            if stop.is_set():
                break
            try:
                fn(project)
                done.append(project)
            except subprocess.CalledProcessError:
                bad.append(project)
            except (Exception, SystemExit) as e:  # noqa: BLE001 - re-raised by the caller
                exc = e
                break
        return done, bad, exc

    def task(chain: _Chain):
        # Runs in a worker thread. Never raises: the sink must make it
        # back to the main thread even when fn() blows up, or the error
        # message written into it would be lost.
        if not buffered:
            return (None, *run_chain(chain))
        with _output.capture() as sink:
            return (sink, *run_chain(chain))

    error: BaseException | None = None

    def collect(chain: _Chain) -> None:
        # Take a finished chain's results over into the caller's lists.
        nonlocal error
        assert chain.future is not None
        chain.collected = True
        sink, done, bad, exc = chain.future.result()
        if sink is not None:
            reporter.finished(sink)
        succeeded.extend(done)
        failed.extend(bad)
        if exc is not None and error is None:
            error = exc

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=jobs, thread_name_prefix='west')
    try:
        # Submitted one by one, so that a Ctrl-C landing here still
        # leaves every started future recorded for the drain below.
        by_future: dict[concurrent.futures.Future, _Chain] = {}
        for chain in work:
            chain.future = executor.submit(task, chain)
            by_future[chain.future] = chain
        pending = set(by_future)

        while pending:
            # Poll with a timeout rather than block: a blocked wait can't
            # be interrupted with Ctrl-C on Windows.
            finished, pending = concurrent.futures.wait(
                pending, timeout=0.25, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in finished:
                if not future.cancelled():
                    collect(by_future[future])
                if error is not None and not stop.is_set():
                    # Stop handing out new work, but keep draining what
                    # is already running so its output gets printed.
                    stop.set()
                    executor.shutdown(wait=False, cancel_futures=True)
    except KeyboardInterrupt:
        # Git got the Ctrl-C too, so the running projects fail quickly;
        # wait for them and show what they printed, then give up.
        stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
        for chain in work:
            if chain.finished and not chain.collected:
                collect(chain)
        raise
    finally:
        executor.shutdown(wait=True)

    if error is not None:
        raise error

    succeeded.sort(key=lambda p: order[id(p)])
    failed.sort(key=lambda p: order[id(p)])
    return succeeded, failed
