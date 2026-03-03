@echo off
chcp 65001 >nul
cd /d "D:\桌面\Bedcode-main"
title BedCode - Telegram Remote Control
echo ============================================
echo   BedCode - Remote Control for Claude Code
echo ============================================
echo.
echo Starting BedCode...
echo.
"D:\minconda-py38\envs\bedcode\python.exe" bot.py
set EXITCODE=%ERRORLEVEL%
echo.
echo ============================================
echo BedCode stopped. Exit code: %EXITCODE%
echo ============================================
pause
