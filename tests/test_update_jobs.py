# Copyright (c) 2026, Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

# Tests for 'west update -j': behaviour that only matters when several
# projects are updated at the same time. The generic update tests in
# test_project.py are parametrized over the job count as well.

import os
import re
import subprocess
import threading
from unittest.mock import patch

import pytest
from conftest import (
    add_commit,
    cmd,
    cmd_raises,
    cmd_subprocess,
    create_repo,
    create_workspace,
    rev_parse,
)

from west import _output
from west.app import _parallel
from west.app.project import Update

BANNER = re.compile(r'^=== updating (\S+) \((\S+)\):$')


def test_jobs_help_and_validation(west_init_tmpdir):
    # (argparse formats the option differently across Python versions)
    out = cmd('update --help')
    assert '--jobs N' in out
    assert '"update.jobs" configuration option' in out
    assert '--buffered, --no-buffered' in out

    _, stderr = cmd_raises('update -j -1', SystemExit)
    assert 'invalid number of jobs -1' in stderr

    # -j takes a value, so a project name right after it is an error
    # (exit code 2 from argparse), not a request for automatic jobs.
    exc_info, _ = cmd_raises('update -j net-tools', SystemExit)
    assert exc_info.value.code == 2

    cmd('config update.jobs auto')
    _, stderr = cmd_raises('update', SystemExit)
    assert 'invalid "update.jobs" configuration value' in stderr


def test_jobs_notice(west_init_tmpdir):
    # The notice line is the visible sign that output will be buffered.
    notice = '--- working on 3 projects with'
    assert notice not in cmd('update')
    assert notice not in cmd('update -j 1')
    assert f'{notice} 2 jobs' in cmd('update -j 2')
    assert f'{notice} 3 jobs' in cmd('update -j 3')

    # 0 means "automatic".
    auto = _parallel.auto_jobs()
    out = cmd('update -j 0')
    if auto > 1:
        assert f'{notice} {auto} jobs' in out
    else:
        assert notice not in out

    # The configuration option is the fallback, and -j overrides it.
    cmd('config update.jobs 2')
    assert f'{notice} 2 jobs' in cmd('update')
    assert notice not in cmd('update -j 1')
    assert f'{notice} 3 jobs' in cmd('update -j 3')

    # With one project there is nothing to run concurrently.
    assert '--- working on' not in cmd('update -j 2 net-tools')


def test_jobs_output_blocks(west_init_tmpdir):
    # With several jobs the output of each project must come out as one
    # contiguous block: a banner, then only that project's lines until
    # the next banner. Run in a subprocess so that git's own output is
    # captured too, and with -v so west's per-command logging is as well.
    out = cmd_subprocess(['-v', 'update', '-j', '3'], stderr=subprocess.STDOUT)

    blocks = {}
    current = None
    for line in out.splitlines():
        match = BANNER.match(line)
        if match:
            current = match.group(1)
            assert current not in blocks, f'{current} updated twice'
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)

    assert sorted(blocks) == ['Kconfiglib', 'net-tools', 'tagged_repo']

    for name, lines in blocks.items():
        assert lines, f'no output for {name}'
        for line in lines:
            if line.startswith('--- '):
                # west's small banners name the project they are about.
                assert line.startswith(f'--- {name}: '), line
            elif "running '" in line:
                # west -v logs every git command with its directory.
                assert f'{os.sep}{name}' in line.replace('/', os.sep), line
        assert any(line.startswith(f'--- {name}: fetching') for line in lines), lines


def test_jobs_failure(west_init_tmpdir):
    # A project that fails to update is reported after everything else
    # has been tried, like it is for a serial update.
    remotes = west_init_tmpdir / '..' / 'repos'
    with open(west_init_tmpdir / 'zephyr' / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          defaults:
            remote: test-local
          remotes:
            - name: test-local
              url-base: {remotes}
          projects:
            - name: net-tools
            - name: broken
              url: file://{west_init_tmpdir}/no-such-remote
            - name: Kconfiglib
              revision: zephyr
              path: subdir/Kconfiglib
          self:
            path: zephyr
        ''')

    for jobs in ('1', '3'):
        exc_info, stderr = cmd_raises(f'update -j {jobs}', SystemExit)
        assert exc_info.value.code == 1
        assert 'update failed for project broken' in stderr

    assert (west_init_tmpdir / 'net-tools').check(dir=1)
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=1)


def test_jobs_duplicate_projects(west_init_tmpdir):
    # Naming a project twice (or by name and by path) must not update
    # it twice at the same time.
    out = cmd('update -j 2 net-tools net-tools subdir/Kconfiglib Kconfiglib')
    assert out.count('=== updating net-tools') == 1
    assert out.count('=== updating Kconfiglib') == 1
    assert (west_init_tmpdir / 'net-tools').check(dir=1)
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=1)


def test_jobs_nested_paths(west_init_tmpdir):
    # A project inside another project's directory is updated after its
    # parent, as a serial update would do.
    remotes = west_init_tmpdir / '..' / 'repos'
    with open(west_init_tmpdir / 'zephyr' / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          defaults:
            remote: test-local
          remotes:
            - name: test-local
              url-base: {remotes}
          projects:
            - name: net-tools
              path: outer
            - name: Kconfiglib
              revision: zephyr
              path: outer/inner
            - name: tagged_repo
              revision: v1.0
          self:
            path: zephyr
        ''')

    cmd('update -j 4')
    assert (west_init_tmpdir / 'outer' / '.git').check(dir=1)
    assert (west_init_tmpdir / 'outer' / 'inner' / '.git').check(dir=1)
    assert (west_init_tmpdir / 'tagged_repo' / '.git').check(dir=1)


def test_jobs_fatal_error_aborts(west_init_tmpdir):
    # Errors other than a failing git command abort the whole update,
    # but the output of the projects that did run is not lost.
    def boom(self, project):
        if project.name == 'net-tools':
            self.die('simulated fatal error')

    with patch.object(Update, 'update_submodules', boom):
        exc_info, stderr = cmd_raises('update -j 3', SystemExit)
    assert exc_info.value.code == 1
    assert 'FATAL ERROR: simulated fatal error' in stderr


def test_jobs_auto_cache_shared_url(tmpdir):
    # Two projects with the same URL share one auto-cache mirror. With
    # several jobs they must not trip over each other creating or
    # refreshing it, on the first and on later updates.
    remote = tmpdir / 'remotes' / 'foo'
    create_repo(remote)
    head = rev_parse(remote, 'HEAD')
    workspace = tmpdir / 'workspace'
    cache = tmpdir / 'cache'

    create_workspace(workspace)
    with open(workspace / 'mp' / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          projects:
          - name: foo
            path: subdir/foo
            url: file://{remote}
            revision: {head}
          - name: foo-again
            url: file://{remote}
            revision: {head}
          - name: foo-branch
            url: file://{remote}
            revision: master
        ''')
    workspace.chdir()

    for _ in range(2):
        cmd(['update', '-j', '3', '--auto-cache', os.fspath(cache)])
        for path in ('subdir/foo', 'foo-again', 'foo-branch'):
            assert rev_parse(workspace / path, 'HEAD') == head
        # One mirror for the one URL.
        assert len([d for d in os.listdir(cache / 'foo') if not d.endswith('.info')]) == 1


def test_jobs_no_buffered(west_init_tmpdir):
    # --no-buffered runs the projects concurrently but prints live, so
    # the per-project blocks are not guaranteed; everything else is.
    out = cmd('update -j 3 --no-buffered')
    assert '--- working on 3 projects with 3 jobs' in out
    for name in ('net-tools', 'Kconfiglib', 'tagged_repo'):
        assert f'=== updating {name}' in out
    assert (west_init_tmpdir / 'net-tools').check(dir=1)
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=1)
    assert (west_init_tmpdir / 'tagged_repo').check(dir=1)


def test_jobs_nested_imports(tmpdir):
    # Projects that provide imports are updated first, one at a time,
    # in the main thread and with live output, however deeply nested
    # the imports are; only what is left is updated concurrently.
    remotes = tmpdir / 'remotes'

    def remote(name, files):
        path = remotes / name
        create_repo(path)
        add_commit(path, f'{name} content', files=files)
        return f'file://{path}'

    alpha = remote('alpha', {'a.txt': 'a'})
    bravo = remote('bravo', {'b.txt': 'b'})
    charlie = remote('charlie', {'c.txt': 'c'})
    inner = remote(
        'inner',
        {
            'west.yml': f'''
            manifest:
              projects:
              - name: alpha
                url: {alpha}
              - name: bravo
                url: {bravo}
            '''
        },
    )
    outer = remote(
        'outer',
        {
            'first.yml': f'''
            manifest:
              projects:
              - name: inner
                url: {inner}
                import: west.yml
              - name: charlie
                url: {charlie}
            '''
        },
    )

    ws = tmpdir / 'ws'
    create_workspace(ws)
    add_commit(
        ws / 'mp',
        'manifest',
        files={
            'west.yml': f'''
            manifest:
              projects:
              - name: outer
                url: {outer}
                import: first.yml
            '''
        },
    )

    records = []
    real_update = Update.update

    def record(self, project):
        records.append(
            (project.name, threading.current_thread().name, _output.current_sink() is not None)
        )
        return real_update(self, project)

    with patch.object(Update, 'update', record):
        cmd('update -j 2', cwd=ws)

    providers, ordinary = records[:2], records[2:]
    assert [name for name, _, _ in providers] == ['outer', 'inner']
    assert all(thread == 'MainThread' and not sunk for _, thread, sunk in providers)
    assert sorted(name for name, _, _ in ordinary) == ['alpha', 'bravo', 'charlie']
    assert all(thread != 'MainThread' and sunk for _, thread, sunk in ordinary)
    for name in ('outer', 'inner', 'alpha', 'bravo', 'charlie'):
        assert (ws / name / '.git').check(dir=1)


@pytest.mark.parametrize('jobs', ['-j 1', '-j 2'])
def test_jobs_stats(west_init_tmpdir, jobs):
    out = cmd(f'update --stats {jobs}')
    assert out.count('performance statistics:') == 3
