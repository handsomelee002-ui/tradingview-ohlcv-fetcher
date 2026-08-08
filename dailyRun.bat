
@echo off

cd /d "C:\Users\inxcllee\git\myProject\tradingview-ohlcv-fetcher"

"C:\Python313\python.exe" "C:\Users\inxcllee\git\myProject\tradingview-ohlcv-fetcher\klse_chart_links.py" >> "C:\Users\inxcllee\git\myProject\tradingview-ohlcv-fetcher\data\task.log" 2>&1

exit /b %errorlevel%

