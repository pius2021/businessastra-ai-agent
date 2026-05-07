#!/bin/bash

echo "[*] Starting FastAPI server..."

uvicorn server:app --host 0.0.0.0 --port $PORT &

sleep 5

echo "[*] Starting LiveKit agent..."

python agent.py start