@echo off
title Clipador - nao feche esta janela
cd /d "%~dp0"
".venv\Scripts\python.exe" app.py
pause
