"""socket.socketpair() over AF_UNIX for Python inside tare's Windows sandbox, where loopback TCP is denied.

tare puts this folder on PYTHONPATH and sets TARE_SANDBOX_SOCKETPAIR=1 only for the commands it runs in the MXC
sandbox. There every loopback connect is refused, so CPython's TCP socketpair fallback, and with it asyncio's event
loop, waits forever. CPython on Windows has no socket.AF_UNIX, so bind/connect/accept go through ws2_32. The socket
file exists for a moment in %TEMP%, which the sandbox replaces with its own folder (bind is refused in a folder the
policy merely grants), and both ends must belong to this process. If anything fails, the original function is used
and the reason goes to stderr once. A sitecustomize further along sys.path still runs.
"""
import os
import sys


def _install():
    import ctypes
    import itertools
    import socket
    from ctypes import wintypes

    ws2 = ctypes.WinDLL('ws2_32', use_last_error=True)
    for call in (ws2.bind, ws2.connect):
        call.argtypes = [ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int]
    ws2.accept.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    ws2.accept.restype = ctypes.c_size_t
    ws2.WSAIoctl.argtypes = [ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    short_path = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
    original, counter, warned = socket.socketpair, itertools.count(), []

    def check(failed, step):
        if failed:
            raise ctypes.WinError(ctypes.get_last_error(), 'AF_UNIX socketpair %s failed' % step)

    def peer_pid(sock):
        pid, size = wintypes.DWORD(), wintypes.DWORD()
        # SIO_AF_UNIX_GETPEERPID: the kernel's record of the peer process, which another process cannot fake.
        check(ws2.WSAIoctl(sock.fileno(), 0x58000100, None, 0, ctypes.byref(pid), 4, ctypes.byref(size), None, None),
              'peer check')
        return pid.value

    def address():
        temp, name = os.environ['TEMP'], '%x-%x' % (os.getpid(), next(counter))
        path = os.path.join(temp, name)
        if len(os.fsencode(path)) > 107:  # sun_path holds 108 bytes with its NUL
            # ponytail: the sandbox's %TEMP% is 87 bytes plus the profile folder's name, and inside it Windows
            # cannot look up a long name's 8.3 form (C:\Users and Packages are not listable), so names over about
            # 11 characters fall back. Upgrade: tare passes the 8.3 form of %LOCALAPPDATA%\Packages in the switch.
            buffer = ctypes.create_unicode_buffer(32768)
            if short_path(temp, buffer, 32768):
                path = os.path.join(buffer.value, name)
        if len(os.fsencode(path)) > 107:
            raise OSError('%%TEMP%% is too long for an AF_UNIX path and has no 8.3 form here: %s' % path)
        return path, b'\x01\x00' + os.fsencode(path) + b'\0'

    def unix_pair():
        path, raw = address()
        listener, client, server = socket.socket(1, socket.SOCK_STREAM), socket.socket(1, socket.SOCK_STREAM), None
        try:
            listener.setblocking(True)  # a default timeout makes new sockets non-blocking; these calls block
            client.setblocking(True)
            check(ws2.bind(listener.fileno(), raw, len(raw)), 'bind')
            try:
                listener.listen(1)
                check(ws2.connect(client.fileno(), raw, len(raw)), 'connect')
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if peer_pid(client) != os.getpid():
                raise OSError('AF_UNIX socketpair reached a listener of another process')
            handle = ws2.accept(listener.fileno(), None, None)
            check(handle == ctypes.c_size_t(-1).value, 'accept')
            server = socket.socket(1, socket.SOCK_STREAM, 0, handle)
            server.set_inheritable(False)
            if peer_pid(server) != os.getpid():
                raise OSError('AF_UNIX socketpair accepted a connection from another process')
        except BaseException:
            client.close()
            if server is not None:
                server.close()
            raise
        finally:
            listener.close()
        for sock in (server, client):
            sock.settimeout(socket.getdefaulttimeout())
        return server, client

    def socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        if type == socket.SOCK_STREAM and not proto:
            try:
                return unix_pair()
            except Exception as error:  # e.g. no %TEMP%: never worse than the original function
                if not warned:
                    warned.append(error)
                    sys.stderr.write('tare sandbox: socket.socketpair falls back to loopback TCP, which the sandbox '
                                     'denies: %r\n' % (error,))
        return original(family, type, proto)

    socket.socketpair = socketpair


def _chain():
    """Run the sitecustomize this one hides, the way site.py would have."""
    import importlib.machinery
    import importlib.util
    here = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.machinery.PathFinder.find_spec('sitecustomize', [
        p for p in sys.path if not isinstance(p, str) or os.path.normcase(os.path.abspath(p or os.curdir)) != here])
    if spec is not None and spec.loader is not None:
        module = importlib.util.module_from_spec(spec)
        sys.modules['sitecustomize'] = module
        spec.loader.exec_module(module)


if sys.platform == 'win32' and os.environ.get('TARE_SANDBOX_SOCKETPAIR') == '1':
    try:
        _install()
    except Exception as error:  # Python must start whatever happens here
        sys.stderr.write('tare sandbox: socket.socketpair shim not installed: %r\n' % (error,))
_chain()
