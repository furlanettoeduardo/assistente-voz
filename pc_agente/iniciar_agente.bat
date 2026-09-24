@echo off
chcp 65001 >nul
rem Inicia o agente do PC com dois cliques. Deixe esta janela aberta enquanto usa a assistente.
title Agente do PC - assistente de voz
cd /d "%~dp0"

set "PYTHON="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYTHON=py -3"
if not defined PYTHON (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON goto sem_python

echo Iniciando o agente. Para parar, feche esta janela ou aperte Ctrl+C.
echo.
%PYTHON% agente.py
set "CODIGO=%ERRORLEVEL%"
echo.
echo O agente parou. Se apareceu um erro acima, corrija e abra este arquivo de novo.
pause
exit /b %CODIGO%

:sem_python
echo Não encontrei o Python neste PC.
echo Instale pelo python.org (versão 3.11 ou mais nova) e abra este arquivo de novo.
pause
exit /b 1
