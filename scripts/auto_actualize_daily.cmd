@echo off
rem A10 daily run for Windows Task Scheduler. Separate tree on origin/dev: the main copy sits on a
rem working branch, and the schedule must not depend on what is open there.
rem Create it once: git worktree add --detach D:\navigator-719-watch origin/dev
rem Git for Windows goes first in PATH: from cmd.exe `bash` otherwise resolves to the WSL stub,
rem and usr\bin holds sed, which the deploy tests need.
set "PATH=C:\Program Files\Git\bin;C:\Program Files\Git\usr\bin;C:\Program Files\Git\cmd;%PATH%"
set NO_PROXY=localhost,127.0.0.1
set WATCH=D:\navigator-719-watch
set SOURCES=D:\navigator-sources
set LOG=%SOURCES%\watch.log
if not exist "%SOURCES%" mkdir "%SOURCES%"
echo === %DATE% %TIME% start >> "%LOG%"
git -C "%WATCH%" fetch -q origin >> "%LOG%" 2>&1 || (echo git fetch failed, running previous code >> "%LOG%")
git -C "%WATCH%" checkout -q --detach origin/dev >> "%LOG%" 2>&1 || (echo git checkout failed >> "%LOG%")
"D:\navigator-719\.venv\Scripts\python.exe" "%WATCH%\scripts\auto_actualize.py" --sources "%SOURCES%" >> "%LOG%" 2>&1
set CODE=%ERRORLEVEL%
echo === exit %CODE% >> "%LOG%"
exit /b %CODE%
