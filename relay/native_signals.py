"""Cooperative CLI cancellation, including a Windows child process group."""
import signal


def install(cancel):
    signals = [signal.SIGINT]
    if hasattr(signal, 'SIGBREAK'):
        signals.append(signal.SIGBREAK)
    previous = {number: signal.getsignal(number) for number in signals}
    for number in signals:
        signal.signal(number, lambda *unused: cancel.cancel())
    return previous


def restore(previous):
    for number, handler in previous.items():
        signal.signal(number, handler)
