@echo off
chcp 65001 >nul
title tare.tools — Distributed Cluster Worker
cls
echo Starting tare.tools Distributed Autonomous Worker Node...
python relay/cluster_worker.py %*
pause
