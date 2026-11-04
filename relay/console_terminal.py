"""OS ownership and permissions for the TUI's long-lived Kernel terminal host."""
import hashlib
import os
from pathlib import Path


def session_owner(custody):
    return 'native:'+hashlib.sha256(os.path.normcase(str(Path(custody).resolve())).encode()).hexdigest()


def child_environment():
    from compute_plane.workspace import isolated_child_environment
    env = isolated_child_environment()
    # A user-authorized system terminal uses their normal Git configuration.
    # These overrides belong to strict Work sandboxes, not this permission mode.
    for name in ('GIT_CONFIG_GLOBAL','GIT_CONFIG_NOSYSTEM','GIT_PAGER','GIT_TERMINAL_PROMPT'):
        if name in os.environ:
            env[name] = os.environ[name]
        else:
            env.pop(name,None)
    for name in ('SSH_AUTH_SOCK','SSH_AGENT_PID','TERM','COLORTERM','LANG','LC_ALL',
                 'HOME','USER','LOGNAME','APPDATA','HOMEDRIVE','HOMEPATH',
                 'PSModulePath','ProgramFiles','ProgramFiles(x86)','ProgramData'):
        if name in os.environ:
            env[name] = os.environ[name]
    env.setdefault('TERM','xterm-256color')
    env['PYTHONUTF8'] = '1'
    return env


class ConsoleTerminals:
    def __init__(self, *, command_mediation_factory=None):
        self._host = None
        self._env = None
        self._monitor = None
        self._connections = {}
        self._projects = {}
        self._command_mediation_factory = command_mediation_factory
        self._command_mediations = {}

    def connection(self, custody, repositories):
        from compute_plane.terminal_host import TerminalHost
        if self._host is None:
            from .terminal_monitor import TerminalMonitor
            self._host = TerminalHost()
            self._env = child_environment()
            try:
                self._monitor = TerminalMonitor(self._host.sessions)
            except BaseException:
                self._host.close(); self._host = None
                raise
        owner = session_owner(custody)
        if self._command_mediation_factory is not None and owner not in self._command_mediations:
            self._command_mediations[owner] = self._command_mediation_factory(custody, repositories)
            if self._command_mediations[owner] is None:
                self._command_mediations.pop(owner)
                raise ValueError('command_mediation_unavailable')
        options = ({'command_mediation': self._command_mediations[owner]}
                   if owner in self._command_mediations else {})
        connection = self._host.bind(owner=owner,repositories=repositories,env=self._env, **options)
        self._connections[owner] = connection
        self._projects[owner] = tuple(repositories)
        self._monitor.add(owner)
        return connection

    def client(self, custody):
        from compute_plane.terminal_host import TerminalClient
        descriptor = self._connections.get(session_owner(custody))
        if descriptor is None:
            raise ValueError('This conversation has no command host in this TUI. Enable commands in a new conversation.')
        return TerminalClient(descriptor)

    def view(self, custody, handle, **kwargs):
        return self._monitor.view(session_owner(custody),handle,**kwargs)

    def input(self, custody, handle, **kwargs):
        return self._monitor.input(session_owner(custody),handle,**kwargs)

    def close(self):
        try:
            if self._monitor is not None: self._monitor.close()
        finally:
            if self._host is not None: self._host.close()


class SessionTerminal:
    """An immutable UI binding to one existing authorized conversation."""
    def __init__(self, terminals, custody):
        self.terminals, self.custody = terminals, custody
        self.client = terminals.client(custody)
        self.title = ', '.join(terminals._projects[session_owner(custody)])+' · '+Path(custody).name[:8]

    def list(self):
        return self.client.request('list',include_completed=True)

    def read(self, handle, cursors=None):
        return self.client.request('read',handle=handle,cursors=cursors or {},limit=16384)

    def send(self, handle, text):
        from uuid import uuid4
        return self.client.request('send',handle=handle,text=text,request_id='operator:'+uuid4().hex)

    def stop(self, handle):
        return self.client.request('stop',handle=handle)

    def eof(self, handle):
        return self.client.request('close_input',handle=handle)

    def resize(self, handle, columns, rows):
        return self.client.request('resize',handle=handle,columns=columns,rows=rows)

    def view(self, handle, **kwargs):
        return self.terminals.view(self.custody,handle,**kwargs)

    def input(self, handle, **kwargs):
        return self.terminals.input(self.custody,handle,**kwargs)


def native_terminal(client, *, custody, config, session_id, binding):
    from compute_plane.native_terminal import NativeTerminal
    from compute_plane.terminal_host import TerminalTransportError
    if client is None:
        raise ValueError('native_terminal_host_required')
    try:
        described = client.request('describe')
    except TerminalTransportError:
        raise ValueError('native_terminal_host_unavailable_reopen_session_host') from None
    if (described.get('owner') != session_owner(custody)
            or described.get('repositories') != config['repositories']
            or described.get('permission') != 'host_commands'):
        raise ValueError('native_terminal_grant_mismatch')
    return NativeTerminal(session_id=session_id,binding=binding,client=client,
                          execution_environment=described.get('execution_environment'))
