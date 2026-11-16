#!/usr/bin/env python3
"""
Waveform Lab HTTP & MIDI Telemetry Server.
Serves static visualizer files and streams categorized live MIDI events
from Akai MPK Mini, Akai APC40 mkII, and Apple Magic Trackpad 2.
Zero external dependencies (pure Python standard library).
"""
import argparse
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

DEFAULT_PORT = 8080
MAX_HISTORY = 60

# Telemetry state
_lock = threading.Lock()
_history: Dict[str, deque] = {
    "mpk": deque(maxlen=MAX_HISTORY),
    "apc": deque(maxlen=MAX_HISTORY),
    "magic_pad": deque(maxlen=MAX_HISTORY),
}
_status: Dict[str, str] = {
    "mpk": "Standby · Ready",
    "apc": "Standby · Ready",
    "magic_pad": "Standby · Ready",
}
_subscribers: Set[queue.Queue] = set()


def _clean_ansi(text: str) -> str:
    """Strip ANSI escape sequences from journal logs."""
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text).strip()


def _parse_and_route_line(unit: str, msg: str):
    """Classify log line into device and extract summary status."""
    clean = _clean_ansi(msg)
    if not clean or clean.startswith("="):
        return

    # Determine device
    dev = None
    if "synth.service" in unit:
        dev = "mpk"
    elif "apc40.service" in unit:
        dev = "apc"
    elif "magicpad.service" in unit:
        dev = "magic_pad"
    else:
        # Fallback keyword matching
        lower = clean.lower()
        if "magic pad" in lower or "linnstrument" in lower:
            dev = "magic_pad"
        elif "apc40" in lower:
            dev = "apc"
        elif "mpk" in lower or "looper" in lower or "pad cc" in lower or "set melódico" in lower or "drum set" in lower:
            dev = "mpk"

    if not dev:
        return

    timestamp = time.strftime("%H:%M:%S")
    entry = {"time": timestamp, "text": clean}

    # Extract dynamic status per device
    status_update = None

    if dev == "mpk":
        if "SET MELÓDICO SELECCIONADO" in clean or "KEYBOARD SELECT" in clean:
            match = re.search(r"(?:->|:)\s*(.+?)(?:\s*\(Prog|\s*$)", clean)
            if match:
                status_update = f"Melodic: {match.group(1).strip()}"
        elif "PAD CC" in clean and ("TAP" in clean or "MANTENIDO" in clean) and "Instrumento" in clean:
            match = re.search(r":\s*(.+?)(?:\s*\(Prog|\s*$)", clean)
            if match:
                status_update = f"Melodic: {match.group(1).strip()}"
        elif "RANDOM" in clean and ("RECALL" in clean or "RE-ROLL" in clean):
            keys_match = re.search(r"Keys:\s*(.+?)(?:\s*\||\s*$)", clean)
            drums_match = re.search(r"Drums:\s*(.+?)$", clean)
            if keys_match:
                status_update = f"Preset: {keys_match.group(1).strip()}"
            elif drums_match:
                status_update = f"Drums: {drums_match.group(1).strip()}"
        elif "RESET DEFAULT COMPLETADO" in clean:
            match = re.search(r"1er Instrumento:\s*(.+?)(?:\s*\||\s*$)", clean)
            if match:
                status_update = f"Melodic: {match.group(1).strip()}"
        elif "DRUM SET SELECCIONADO" in clean:
            match = re.search(r"Kit \d+:\s*(.+?)(?:\s*\(Kit|\s*$)", clean)
            if match:
                status_update = f"Drums: {match.group(1).strip()}"
        elif "LOOPER" in clean and ("CONFIRMADOS" in clean or "COMPASES" in clean or "BPM" in clean):
            match = re.search(r"(\d+\s*compás.*?BPM|MODO LIBRE.*?BPM|\d+\s*BPM)", clean)
            if match:
                status_update = f"Looper: {match.group(1).strip()}"
        elif "KNOB 3 - TEMPO" in clean:
            match = re.search(r"BPM:\s*(\d+)", clean)
            if match:
                status_update = f"Tempo: {match.group(1)} BPM"

    elif dev == "apc":
        if "APC40 MODE" in clean:
            match = re.search(r"MODE \d+:\s*(.+?)(?:\s*\(|\s*$)", clean)
            if match:
                status_update = f"Mode: {match.group(1).strip()}"
        elif "DRUM KIT" in clean:
            match = re.search(r"->\s*(.+?)$", clean)
            if match:
                status_update = f"Drums: {match.group(1).strip()}"
        elif "METRONOME" in clean or "TAP TEMPO" in clean:
            match = re.search(r"(\d+)\s*BPM", clean)
            if match:
                status_update = f"Tempo: {match.group(1)} BPM"

    elif dev == "magic_pad":
        if "MODO ->" in clean:
            match = re.search(r"MODO ->\s*(.+?)$", clean)
            if match:
                status_update = f"Mode: {match.group(1).strip()}"
        elif "Sonido (" in clean:
            match = re.search(r"->\s*(.+?)$", clean)
            if match:
                status_update = f"Sound: {match.group(1).strip()}"
        elif "VOLUMEN SESIÓN" in clean:
            match = re.search(r"maestro:\s*(\d+%)", clean)
            if match:
                status_update = f"Volume: {match.group(1).strip()}"

    # Broadcast to memory and subscribers
    with _lock:
        _history[dev].append(entry)
        if status_update:
            _status[dev] = status_update
        active_status = _status[dev]
        subs = list(_subscribers)

    payload = json.dumps({
        "device": dev,
        "entry": entry,
        "status": active_status,
    })

    for client_queue in subs:
        try:
            client_queue.put_nowait(payload)
        except queue.Full:
            pass  # Drop if client queue is saturated to prevent blocking the reader


def _journal_listener_thread():
    """Background thread following systemd journal for the 3 controllers."""
    cmd = [
        "journalctl",
        "-q",
        "-u", "synth.service",
        "-u", "apc40.service",
        "-u", "magicpad.service",
        "-f",
        "-n", "30",
        "-o", "json",
    ]

    while True:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                bufsize=1,
            )
            if not proc.stdout:
                time.sleep(5)
                continue

            for raw_line in proc.stdout:
                if not raw_line.strip():
                    continue
                try:
                    data = json.loads(raw_line)
                    unit = data.get("_SYSTEMD_UNIT", "") or data.get("_SYSTEMD_USER_UNIT", "")
                    msg = data.get("MESSAGE", "")
                    if msg:
                        _parse_and_route_line(unit, msg)
                except Exception:
                    continue

            proc.wait()
        except FileNotFoundError:
            # Not on Linux / systemd, sleep gracefully
            time.sleep(10)
        except Exception:
            time.sleep(3)


class MidiStreamHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.directory = os.path.dirname(os.path.abspath(__file__))
        super().__init__(*args, directory=self.directory, **kwargs)

    def end_headers(self):
        # Enable CORS for local network and VPN access
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/midi/state":
            with _lock:
                state = {
                    "online": True,
                    "status": dict(_status),
                    "logs": {dev: list(_history[dev]) for dev in _history},
                }
            raw = json.dumps(state).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(raw)
            return

        elif parsed.path == "/api/midi/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            client_queue = queue.Queue(maxsize=100)
            with _lock:
                _subscribers.add(client_queue)

            # Send initial connected greeting and stream events from queue
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event_data = client_queue.get(timeout=15.0)
                        msg = f"data: {event_data}\n\n".encode("utf-8")
                        self.wfile.write(msg)
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError, socket.error):
                pass
            finally:
                with _lock:
                    _subscribers.discard(client_queue)
            return

        # Serve static visualizer assets
        super().do_GET()

    def log_message(self, fmt, *args):
        # Silence routine static asset spam in terminal
        if "GET /api/" in (args[0] if args else ""):
            return
        # super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser(description="Waveform Lab Web & MIDI Telemetry Server")
    parser.add_argument("--port", "-p", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
                        help=f"Port to listen on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    # Start journalctl background follower
    t = threading.Thread(target=_journal_listener_thread, daemon=True)
    t.start()

    server_address = ("0.0.0.0", args.port)
    httpd = ThreadingHTTPServer(server_address, MidiStreamHandler)
    print(f"==================================================================")
    print(f" Waveform Lab - Audio Visualizer & Multi-Device MIDI Telemetry   ")
    print(f" Server running at http://0.0.0.0:{args.port}/                   ")
    print(f" Endpoints: /api/midi/state, /api/midi/stream                     ")
    print(f"==================================================================", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()


if __name__ == "__main__":
    main()

