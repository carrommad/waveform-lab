#!/usr/bin/env bash
# Quick launcher for Waveform Lab & MIDI Telemetry Server
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8080}"

echo "Starting Waveform Lab on port $PORT..."
if command -v python3 &>/dev/null; then
  exec python3 "$DIR/server.py" --port "$PORT"
else
  echo "Error: python3 is required to run server.py."
  exit 1
fi
