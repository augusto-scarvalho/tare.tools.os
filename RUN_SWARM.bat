@echo off
chcp 65001 >nul
title tare.tools — Autonomous Multi-Agent Swarm
cls
echo Starting tare.tools Multi-Agent Relay Mesh (MARM)...
python relay/start_swarm.py %*
pause
