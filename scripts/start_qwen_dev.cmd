@echo off
setlocal
title Linguistic Online Judge - Qwen9B private connection
echo Real Qwen development website: http://127.0.0.1:8090/
echo Keep this window open. The private school application must already be running.
echo This forwards the website port, not the model API. No model is installed locally.
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:8090:127.0.0.1:8090 75
echo.
echo The private connection ended. Review any messages above.
pause
