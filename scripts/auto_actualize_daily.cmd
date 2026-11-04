@echo off
rem A10 daily run for Windows Task Scheduler. Separate tree on origin/dev: the main copy sits on a
rem working branch, and the schedule must not depend on what is open there.
rem Create it once: git worktree add --detach D:\navigator-719-watch origin/dev
rem Git for Windows goes first in PATH: from cmd.exe `bash` otherwise resolves to the WSL stub,
rem and usr\bin holds sed, which the deploy tests need.
rem
rem 04.11.2026 - the first scheduled run died 4 s after start, 0xC000013A: the task is Interactive, so it
rem opened a console window right after the PC woke up, and the window got closed. The task starts this
rem file HIDDEN, the action is:
rem   powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -Command
rem     "& cmd.exe /c 'D:\navigator-719-watch\scripts\auto_actualize_daily.cmd'; exit $LASTEXITCODE"
rem conhost --headless does not do: it loses the exit code.
rem
rem THE BODY IS ONE PARENTHESIZED BLOCK, KEEP IT SO. `git checkout` below rewrites this very file, and cmd
rem reads a batch file by byte offset: outside a block it goes on reading the NEW file at the OLD offset
rem (checked 04.11: fragments of the new file ran, exit code 0 instead of the real one). A block is read
rem whole before it runs, and `exit /b` inside it never returns to the file. Inside the block: no labels,
rem no goto, no unbalanced parentheses in echo texts; values set inside are read as !VAR!.
setlocal EnableDelayedExpansion
set "PATH=C:\Program Files\Git\bin;C:\Program Files\Git\usr\bin;C:\Program Files\Git\cmd;%PATH%"
set NO_PROXY=localhost,127.0.0.1
rem Unbuffered: a run stopped half-way keeps in the log everything it printed.
set PYTHONUNBUFFERED=1
rem A stalled HTTPS transfer of any git call - fetch here, fetch/ls-remote/push inside the script -
rem aborts after 60 s below 1 KB/s instead of hanging until the 2 h task limit.
set GIT_HTTP_LOW_SPEED_LIMIT=1000
set GIT_HTTP_LOW_SPEED_TIME=60
set WATCH=D:\navigator-719-watch
set SOURCES=D:\navigator-sources
set LOG=%SOURCES%\watch.log
if not exist "%SOURCES%" mkdir "%SOURCES%"
(
  echo === %DATE% %TIME% start >> "%LOG%"
  git -C "%WATCH%" fetch -q origin >> "%LOG%" 2>&1 || echo git fetch failed, running previous code >> "%LOG%"
  git -C "%WATCH%" checkout -q --detach origin/dev >> "%LOG%" 2>&1 || echo git checkout failed >> "%LOG%"
  "D:\navigator-719\.venv\Scripts\python.exe" "%WATCH%\scripts\auto_actualize.py" --sources "%SOURCES%" >> "%LOG%" 2>&1
  set CODE=!ERRORLEVEL!
  echo === exit !CODE! >> "%LOG%"
  exit /b !CODE!
)
