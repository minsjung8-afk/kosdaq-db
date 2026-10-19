@echo off
REM 최초 1회: npm install -g wrangler  &  wrangler login
cd /d %~dp0
python run.py site || exit /b 1
wrangler pages deploy site --project-name kosdaq-db --commit-dirty=true
