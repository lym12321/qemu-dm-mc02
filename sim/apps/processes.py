"""Explicitly trusted host programs borrowed by a finite simulation app.

No shell, scheduler or simulated clock. Simulation owns this component's
lifecycle; each child owns one OS process group. Admission is resource-free.
"""

from dataclasses import dataclass
from collections.abc import Mapping
import os
from pathlib import Path
import shutil
import subprocess

from sim.api import NodeDescription
from sim.config.yaml import ManifestError, fields, identifier, text_bytes
from sim.machines.qemu.process import stop
from sim.processes import ChildProcess


@dataclass(frozen=True)
class HostCommand:
    name: str
    command: tuple[str, ...]
    cwd: Path


def _text(value, label, *, empty=False):
    result = text_bytes(value, label, empty=empty, maximum=4096)
    if '\0' in value:
        raise ManifestError(f'{label} must not contain NUL')
    return result


def admit_commands(value, cwd):
    """Resolve literal argv using the same cwd/PATH semantics as Popen."""
    if not isinstance(value, Mapping):
        raise ManifestError('commands must be a mapping with 1..8 entries')
    if not 1 <= len(value) <= 8:
        raise ManifestError('commands must be a mapping with 1..8 entries')
    result = []
    for name, configuration in value.items():
        identifier(name, 'command name')
        item = fields(dict(configuration), {'command'}, {'command'}, name)
        argv = item['command']
        if not isinstance(argv, (list, tuple)) or not 1 <= len(argv) <= 128:
            raise ManifestError('command must contain 1..128 arguments')
        for index, argument in enumerate(argv):
            _text(argument, f'{name}.command[{index}]', empty=index > 0)
        if not argv[0].strip():
            raise ManifestError('command executable must not be blank')
        if '/' in argv[0]:
            executable = cwd / argv[0]
        else:
            search = os.pathsep.join(str(cwd / directory) for directory in os.get_exec_path())
            found = shutil.which(argv[0], path=search)
            if found is None:
                raise ManifestError(f'command executable is not found in PATH: {argv[0]}')
            executable = Path(found)
        # Preserve the launch spelling: dereferencing a venv's Python symlink
        # selects the system environment and silently loses its dependencies.
        executable = Path(os.path.abspath(executable))
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ManifestError('command executable must be an executable regular file')
        command = (str(executable), *argv[1:])
        size = sum(len(_text(arg, f'{name}.command', empty=i > 0)) + 1
                   for i, arg in enumerate(command))
        if size > 16 * 1024:
            raise ManifestError('command exceeds 16 KiB including argument terminators')
        result.append(HostCommand(name, command, cwd))
    return tuple(result)


class HostProcesses:
    def __init__(self, commands, context):
        self.commands, self.context = commands, context
        self.children = []
        self.closed = self.started = False

    def start(self):
        if self.started or self.closed:
            raise RuntimeError('host programs can only start once')
        self.started = True
        for command in self.commands:
            self.context.check_deadline()
            process = subprocess.Popen(command.command, cwd=command.cwd, shell=False,
                                       start_new_session=True, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child = ChildProcess(command.name, process)
            self.children.append(child)
            child.prepare()
            self.context.emit(None, 'process.started', {'name': command.name, 'pid': process.pid})
            self.context.check_deadline()

    def check_health(self):
        # Deadline checks may run many times per QMP command. Never drain pipes,
        # emit events or call back into Simulation from this synchronous hook.
        if not self.closed:
            for child in self.children:
                result = child.process.poll()
                if result is not None:
                    raise RuntimeError(f'host process {child.label} exited ({result}): '
                                       f'{child.diagnostics()}')

    def sample(self, cut):
        for child in self.children:
            previous = dict(child.tails)
            child.pump()
            if child.tails != previous:
                self.context.emit(None, 'process.output',
                                  {'name': child.label, **child.diagnostics()})
        self.check_health()

    def close(self):
        if self.closed:
            return
        self.closed = True
        errors = []
        for child in reversed(self.children):
            try:
                stop(child.process)
            except BaseException as error:
                errors.append(error)
            for name in ('stdout', 'stderr'):
                try:
                    getattr(child.process, name).close()
                except BaseException as error:
                    errors.append(error)
        if errors:
            primary, *others = errors
            for other in others:
                primary.add_note(f'host process cleanup: {type(other).__name__}: {other}')
            raise primary


class HostProcessesFactory:
    def __init__(self, *, allow_launch=False):
        if type(allow_launch) is not bool:
            raise TypeError('allow_launch must be a boolean')
        self.allow_launch = allow_launch

    def _commands(self, spec):
        fields(dict(spec.options), {'commands'}, {'commands'}, spec.name)
        return admit_commands(spec.options['commands'], spec.base_dir)

    def describe(self, spec):
        self._commands(spec)
        return NodeDescription(capabilities={'health', 'sample'})

    def validate_topology(self, topology, descriptions, registry):
        count = sum(len(self._commands(node)) for node in topology.nodes
                    if isinstance(registry[node.kind], HostProcessesFactory))
        if count > 8:
            raise ValueError('a simulation permits at most 8 host processes')

    def create(self, spec, context):
        if not self.allow_launch:
            raise ValueError('host processes require explicit --allow-host-processes or factory opt-in')
        return HostProcesses(self._commands(spec), context)
