@echo off
rem Inicia o watcher de resultados SEM janela (pyw.exe), no intervalo POLL_SECONDS da config.
rem Antes: ajuste WATCH_TICKERS / WATCH_LABEL / SPEAK_NAMES / POLL_SECONDS em earnings_watch.py.
rem Argumentos extras passam adiante (ex.: iniciar_earnings_watch.cmd --poll 20).
rem Confirmacao: pop-up "Watcher ... iniciado" + Telegram. Acompanhar: py earnings_watch.py --status
rem Parar: py earnings_watch.py --stop
start "" pyw "%~dp0earnings_watch.py" %*
