# Copyright (c) 2025 Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

import textwrap

import pytest
from conftest import add_commit, cmd

from west.commands import ExtensionCommandError, extension_commands
from west.configuration import Configuration
from west.manifest import Manifest


def test_extension_commands_basic(west_init_tmpdir):
    # Test basic extension command loading and structure
    cmd('update')

    config = Configuration(topdir=west_init_tmpdir)
    manifest = Manifest.from_topdir(west_init_tmpdir)

    ext_specs = extension_commands(config, manifest)

    # Should have one project with extension commands (net-tools)
    assert len(ext_specs) == 1
    assert 'net-tools' in ext_specs

    # Check the extension command spec
    specs = ext_specs['net-tools']
    assert len(specs) == 1
    spec = specs[0]

    assert spec.name == 'test-extension'
    assert spec.help == 'test-extension-help'
    assert spec.project.name == 'net-tools'
    assert spec.factory.name == 'test-extension'
    assert spec.factory.attr == 'TestExtension'
    assert spec.factory.py_file.endswith('scripts/test.py')


def test_extension_commands_disabled(west_init_tmpdir):
    # Test that extension commands can be disabled via config
    cmd('update')
    cmd('config commands.allow_extensions false')

    config = Configuration(topdir=west_init_tmpdir)
    manifest = Manifest.from_topdir(west_init_tmpdir)

    ext_specs = extension_commands(config, manifest)

    # Should be empty when disabled
    assert len(ext_specs) == 0


def test_extension_command_factory(west_init_tmpdir):
    # Test that extension command factory creates command instances
    cmd('update')

    config = Configuration(topdir=west_init_tmpdir)
    manifest = Manifest.from_topdir(west_init_tmpdir)

    ext_specs = extension_commands(config, manifest)
    spec = ext_specs['net-tools'][0]

    # Call the factory to create a command instance
    command = spec.factory()

    assert command.name == 'test-extension'
    assert command.help == 'test-extension-help'
    assert hasattr(command, 'do_add_parser')
    assert hasattr(command, 'do_run')


def test_extension_command_missing_file(repos_tmpdir):
    # Test handling of extension commands with missing python files
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    # Initialize workspace
    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Replace west-commands.yml with one that references a non-existent file
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add broken extension command',
        files={
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/nonexistent.py
                    commands:
                      - name: broken-cmd
                        class: BrokenCommand
                        help: this will fail
                '''),
        },
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)

    # Should get extension command specs even though the python file doesn't exist
    # (python file existence is not checked during spec loading, only when factory is called)
    ext_specs = extension_commands(config, manifest)

    # net-tools should be in the result with the spec created
    assert 'net-tools' in ext_specs
    assert len(ext_specs['net-tools']) == 1

    # But trying to instantiate it should fail with FileNotFoundError
    # (not wrapped in ExtensionCommandError because the factory only catches ImportError)
    spec = ext_specs['net-tools'][0]
    with pytest.raises(FileNotFoundError):
        spec.factory()


def test_extension_command_invalid_yaml(repos_tmpdir):
    # Test handling of invalid YAML in west-commands file
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Replace with invalid YAML
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add invalid yaml',
        files={
            'scripts/west-commands.yml': '[[[ this is not valid YAML at all',
        },
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)

    # Should raise ExtensionCommandError when trying to load extension commands
    with pytest.raises(ExtensionCommandError):
        extension_commands(config, manifest)


def test_extension_command_invalid_schema(repos_tmpdir):
    # Test handling of YAML that doesn't match the schema
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Replace with YAML with invalid schema (missing required 'file' field)
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add invalid schema',
        files={
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - commands:
                      - name: bad-cmd
                '''),
        },
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)

    # Should raise ExtensionCommandError
    with pytest.raises(ExtensionCommandError):
        extension_commands(config, manifest)


def test_extension_command_missing_attribute(repos_tmpdir):
    # Test handling of extension command with missing class attribute
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Create a python file without the expected class
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add python file without class',
        files={
            'scripts/no-class.py': textwrap.dedent('''\
                # This file doesn't have the expected class
                def some_function():
                    pass
                '''),
            'scripts/no-class.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/no-class.py
                    commands:
                      - name: no-class-cmd
                        class: MissingClass
                        help: this will fail
                '''),
        },
    )

    # Update manifest
    manifest_content = (workspace / 'zephyr' / 'west.yml').read()
    manifest_content = manifest_content.replace(
        'west-commands: scripts/west-commands.yml', 'west-commands: scripts/no-class.yml'
    )
    add_commit(
        workspace / 'zephyr',
        'reference no-class yaml',
        files={'west.yml': manifest_content},
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)
    ext_specs = extension_commands(config, manifest)

    # Factory should raise ExtensionCommandError when called
    spec = ext_specs['net-tools'][0]
    with pytest.raises(ExtensionCommandError) as exc_info:
        spec.factory()

    assert 'no attribute MissingClass' in str(exc_info.value.hint)


def test_extension_command_constructor_error(repos_tmpdir):
    # Test handling of extension command whose constructor raises an exception
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Create a command class with a broken constructor
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add command with broken constructor',
        files={
            'scripts/broken-ctor.py': textwrap.dedent('''\
                from west.commands import WestCommand
                class BrokenConstructor(WestCommand):
                    def __init__(self):
                        raise ValueError("Constructor intentionally broken")
                    def do_add_parser(self, parser_adder):
                        pass
                    def do_run(self, args, unknown):
                        pass
                '''),
            'scripts/broken-ctor.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/broken-ctor.py
                    commands:
                      - name: broken-ctor
                        class: BrokenConstructor
                        help: broken constructor
                '''),
        },
    )

    # Update manifest
    manifest_content = (workspace / 'zephyr' / 'west.yml').read()
    manifest_content = manifest_content.replace(
        'west-commands: scripts/west-commands.yml', 'west-commands: scripts/broken-ctor.yml'
    )
    add_commit(
        workspace / 'zephyr',
        'reference broken-ctor yaml',
        files={'west.yml': manifest_content},
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)
    ext_specs = extension_commands(config, manifest)

    # Factory should raise ExtensionCommandError
    spec = ext_specs['net-tools'][0]
    with pytest.raises(ExtensionCommandError) as exc_info:
        spec.factory()

    assert 'command constructor threw an exception' in str(exc_info.value.hint)


def test_extension_command_import_error(repos_tmpdir):
    # Test handling of extension command with import errors
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Create a python file with an import error
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add file with import error',
        files={
            'scripts/import-error.py': textwrap.dedent('''\
                from nonexistent_module import something
                from west.commands import WestCommand
                class TestCommand(WestCommand):
                    pass
                '''),
            'scripts/import-error.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/import-error.py
                    commands:
                      - name: import-error
                        class: TestCommand
                        help: import error
                '''),
        },
    )

    # Update manifest
    manifest_content = (workspace / 'zephyr' / 'west.yml').read()
    manifest_content = manifest_content.replace(
        'west-commands: scripts/west-commands.yml', 'west-commands: scripts/import-error.yml'
    )
    add_commit(
        workspace / 'zephyr',
        'reference import-error yaml',
        files={'west.yml': manifest_content},
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)
    ext_specs = extension_commands(config, manifest)

    # Factory should raise ExtensionCommandError
    spec = ext_specs['net-tools'][0]
    with pytest.raises(ExtensionCommandError) as exc_info:
        spec.factory()

    assert 'could not import' in str(exc_info.value.hint)


def test_extension_command_directory_escape(repos_tmpdir):
    # Test that extension commands can't escape project directory
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Replace west-commands.yml with one that tries to reference a file outside the project
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add escaping west-commands file',
        files={
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: ../../zephyr/evil.py
                    commands:
                      - name: evil
                        class: Evil
                        help: escape attempt
                '''),
        },
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)

    # Should raise ExtensionCommandError when loading extension commands
    with pytest.raises(ExtensionCommandError) as exc_info:
        extension_commands(config, manifest)

    assert 'escapes project path' in str(exc_info.value.hint)


def test_extension_command_spec_file_escape(repos_tmpdir):
    # Test that west-commands spec file path can't escape project directory
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Create an evil-commands.yml file that exists but in the wrong place
    (workspace / 'evil-commands.yml').write('west-commands: []')

    # Update manifest with west-commands path that tries to escape
    manifest_content = (workspace / 'zephyr' / 'west.yml').read()
    manifest_content = manifest_content.replace(
        'west-commands: scripts/west-commands.yml', 'west-commands: ../../evil-commands.yml'
    )
    add_commit(
        workspace / 'zephyr',
        'add escaping west-commands path',
        files={'west.yml': manifest_content},
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)

    # Should raise ExtensionCommandError when trying to load extension commands
    with pytest.raises(ExtensionCommandError) as exc_info:
        extension_commands(config, manifest)

    assert 'escapes project path' in str(exc_info.value.hint)


def test_extension_command_default_class_name(repos_tmpdir):
    # Test that class name defaults to command name if not specified
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Replace west-commands.yml with command without explicit class name
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add command with default class name',
        files={
            'scripts/test.py': textwrap.dedent('''\
                from west.commands import WestCommand
                class mycommand(WestCommand):
                    def __init__(self):
                        super().__init__('mycommand', 'help text', 'description')
                    def do_add_parser(self, parser_adder):
                        return parser_adder.add_parser(self.name)
                    def do_run(self, args, unknown):
                        print('default class name works')
                '''),
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/test.py
                    commands:
                      - name: mycommand
                        help: test default class name
                '''),
        },
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)
    ext_specs = extension_commands(config, manifest)

    # Find the mycommand spec
    mycommand_spec = None
    for specs in ext_specs.values():
        for spec in specs:
            if spec.name == 'mycommand':
                mycommand_spec = spec
                break

    assert mycommand_spec is not None
    assert mycommand_spec.factory.attr == 'mycommand'

    # Should be able to instantiate it
    command = mycommand_spec.factory()
    assert command.name == 'mycommand'


def test_extension_command_default_help(repos_tmpdir):
    # Test that help text has a default if not specified
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Replace west-commands.yml with command without help text
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add command without help',
        files={
            'scripts/test.py': textwrap.dedent('''\
                from west.commands import WestCommand
                class NoHelp(WestCommand):
                    def __init__(self):
                        super().__init__('nohelp', 'help', 'description')
                    def do_add_parser(self, parser_adder):
                        return parser_adder.add_parser(self.name)
                    def do_run(self, args, unknown):
                        pass
                '''),
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/test.py
                    commands:
                      - name: nohelp
                        class: NoHelp
                '''),
        },
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)
    ext_specs = extension_commands(config, manifest)

    # Find the nohelp spec
    nohelp_spec = None
    for specs in ext_specs.values():
        for spec in specs:
            if spec.name == 'nohelp':
                nohelp_spec = spec
                break

    assert nohelp_spec is not None
    # Should have default help text
    assert 'no help provided' in nohelp_spec.help
    assert 'try "west nohelp -h"' in nohelp_spec.help


def test_extension_command_multiple_commands_same_file(repos_tmpdir):
    # Test multiple commands defined in the same python file
    workspace = repos_tmpdir / 'workspace'
    manifest_repo = repos_tmpdir / 'repos' / 'zephyr'

    cmd(['init', '-m', str(manifest_repo), str(workspace)])
    workspace.chdir()
    cmd('update')

    # Create file with multiple command classes
    net_tools_path = workspace / 'net-tools'
    add_commit(
        net_tools_path,
        'add multiple commands',
        files={
            'scripts/multi.py': textwrap.dedent('''\
                from west.commands import WestCommand

                class FirstCommand(WestCommand):
                    def __init__(self):
                        super().__init__('first', 'first help', 'first description')
                    def do_add_parser(self, parser_adder):
                        return parser_adder.add_parser(self.name)
                    def do_run(self, args, unknown):
                        print('first command')

                class SecondCommand(WestCommand):
                    def __init__(self):
                        super().__init__('second', 'second help', 'second description')
                    def do_add_parser(self, parser_adder):
                        return parser_adder.add_parser(self.name)
                    def do_run(self, args, unknown):
                        print('second command')
                '''),
            'scripts/multi.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/multi.py
                    commands:
                      - name: first
                        class: FirstCommand
                        help: first command help
                      - name: second
                        class: SecondCommand
                        help: second command help
                '''),
        },
    )

    # Update manifest
    manifest_content = (workspace / 'zephyr' / 'west.yml').read()
    manifest_content = manifest_content.replace(
        'west-commands: scripts/west-commands.yml', 'west-commands: scripts/multi.yml'
    )
    add_commit(
        workspace / 'zephyr',
        'reference multi yaml',
        files={'west.yml': manifest_content},
    )

    config = Configuration(topdir=workspace)
    manifest = Manifest.from_topdir(workspace)
    ext_specs = extension_commands(config, manifest)

    # Should have both commands
    assert len(ext_specs['net-tools']) == 2

    names = {spec.name for spec in ext_specs['net-tools']}
    assert 'first' in names
    assert 'second' in names

    # Both should instantiate correctly
    for spec in ext_specs['net-tools']:
        command = spec.factory()
        assert command.name in ['first', 'second']
