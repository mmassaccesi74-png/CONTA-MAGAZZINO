@echo off
rem Avvia il programma Acquisti SAP (deve stare nella cartella del programma Etichette SAP)
cd /d "%~dp0"
py acquisti.py || python acquisti.py
if errorlevel 1 pause
