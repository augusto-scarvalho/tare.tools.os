@echo off
chcp 65001 >nul
title tare.tools — Cluster Coordinator & Cockpit
cls
echo Starting tare.tools Cluster Coordinator & Cockpit Web Server...
python relay/cluster_coordinator.py
pause
