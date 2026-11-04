# Agent commands on Windows

OS 0.1.51 / Kernel 0.9.19. The terminal tells the agent the host operating system,
the available shell, the startup arguments, the executables found on the authorized
PATH and short execution rules. It does not send environment variables or
run probes. The facts are refreshed in the system context on each
invocation, without changing the persisted identity of earlier sessions.

On Windows, the preferred shell is PowerShell 7, Windows PowerShell or cmd, in that
order, depending on availability. Simple commands still use argv directly.
The PowerShell prefix sets the process's literal directory and UTF-8 output.
The instructions and the `Join-Path` example cover absolute paths with `-LiteralPath`, reading
UTF-8 files with `-Encoding UTF8`, propagating exit codes and PS 5.1 syntax.

`terminal_read` tells an expired wait (`read_wait_expired`, process still
running) apart from the process limit (`process_timed_out`). While the process is
running, it tells the agent to query the same handle. An interrupted process keeps
its partial output, reason and cleanup confirmation; it is not repeated automatically.

## Qualification

Regression tests drive the TUI by keyboard, through the backend subprocess and the
real command host; only inference is simulated. They cover three turns in the same
session, a folder with spaces and brackets, a file with accented characters, exit code 7, completion after
a wait and termination by timeout without repeating the process. Kernel tests also cover
handle persistence, partial output, isolation and process cleanup.

The first trial with GPT-6 Luna ran both scripts correctly, but it did not
receive the host facts. The following trials found and fixed two
Windows PowerShell quirks: a wrong starting directory when the
path contains brackets, and ANSI reading of UTF-8 files without a BOM. The final script
uses the installed product and checks the results recorded by the terminal,
including the number of runs, instead of trusting only the model's answer.

Result on 2026-09-23: 61 Kernel tests and 15 OS/TUI tests passed; two
POSIX tests were skipped on Windows. The context and paths test passed again
after the final adjustment of the example. The real TUI, driven by keyboard automation and
connected to GPT-6 Luna, completed three turns with 17 model calls and exactly
four processes: exit codes 3, 0 and 7 and one intentional timeout. Accented characters were preserved,
no command was repeated and the cleanup of all four processes was confirmed.

The additional context measured 2,500 bytes on the qualified host. The final installation is
`b58539961d0583e082e214c8fd65b17a347a5669491cf301396cf6e7f5cfc1a9`, built
from OS `92bc2fd` and Kernel `e87c872`; the five libraries produced identical
wheels in two clean builds. `qualification.json` records the checks;
`installed-final/tui.svg` records the final screen. The earlier trials were
preserved, including the empty read with a relative path that led to the explicit
`Join-Path` example in the context and in the tool description.

The raw evidence of this run is not published. The qualification script used a
disposable project and did not modify the operational backlog.
