@echo off
cd /d C:\Users\Will\Documents\GitHub\companies-house-leads
python .\scripts\enrichment\ch_monitor_progress.py --db .\companies-house.db --json-log .\logs\overnight-full-group.jsonl --output-jsonl .\logs\overnight-full-group-hourly-snapshots.jsonl --alerts-jsonl .\logs\overnight-full-group-alerts.jsonl --state-json .\logs\overnight-full-group-monitor-state.json
