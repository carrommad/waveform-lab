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
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

DEFAULT_PORT = 8080
MAX_HISTORY = 300

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
        if "PRESET BEAT ▶" in clean:
            match = re.search(r"'(.*?)'\s*a\s*(\d+\s*BPM)", clean)
            if match:
                status_update = f"Preset: '{match.group(1)}' ({match.group(2)})"
        elif "PRESET BEAT ⏹" in clean:
            match = re.search(r"'(.*?)'\s*detenido", clean)
            status_update = f"Preset: '{match.group(1)}' Detenido" if match else "Preset: Detenido"
        elif "RANDOM" in clean and ("RECALL" in clean or "RE-ROLL" in clean):
            keys_match = re.search(r"Keys:\s*\[?(?:[\d\.]+\s*)?([^\]]+?)\]?\s*(.+?)(?:\s*\(Prog|\s*\|)", clean)
            drums_match = re.search(r"Drums:\s*Kit\s*\d+\s*(.+?)(?:\s*\(Kit|\s*$)", clean)
            k_name = keys_match.group(2).strip() if keys_match else ""
            d_name = drums_match.group(1).strip() if drums_match else ""
            if k_name and d_name:
                status_update = f"Random: 🎹 {k_name} | 🥁 {d_name}"
            elif k_name:
                status_update = f"Random Keys: {k_name}"
            elif d_name:
                status_update = f"Random Drums: {d_name}"
        elif "SET MELÓDICO SELECCIONADO" in clean:
            match = re.search(r"->\s*([\d\.]+\s*.+?)\s*(?:\(1er Sonido:\s*(.+?)\)|\(|$)", clean)
            if match:
                set_name = match.group(1).strip()
                sound_name = match.group(2).strip() if match.group(2) else ""
                status_update = f"Keys: {sound_name} ({set_name})" if sound_name else f"Keys Set: {set_name}"
        elif "KEYBOARD SELECT" in clean:
            match = re.search(r"Tecla \d+:\s*(.+?)(?:\s*\(Prog|\s*$)", clean)
            if match:
                status_update = f"Keys: {match.group(1).strip()}"
        elif "PAD CC" in clean and ("TAP" in clean or "MANTENIDO" in clean):
            if "Instrumento" in clean:
                match = re.search(r":\s*(.+?)(?:\s*\(Prog|\s*$)", clean)
                if match:
                    status_update = f"Keys: {match.group(1).strip()}"
            elif "Set Siguiente" in clean or "Set Anterior" in clean or "Set" in clean:
                match = re.search(r"Set\s*(?:Siguiente|Anterior)?:\s*(.+?)(?:\s*\(|\s*$)", clean)
                if match:
                    status_update = f"Keys Set: {match.group(1).strip()}"
        elif "DRUM SET SELECCIONADO" in clean:
            match = re.search(r"Kit \d+:\s*(.+?)(?:\s*\(Kit|\s*$)", clean)
            if match:
                status_update = f"Drums: {match.group(1).strip()}"
        elif "NUEVOS INSTRUMENTOS FIJADOS" in clean:
            status_update = "Looper: Voces Fijadas 🔒"
        elif "RE-VOICE MODE" in clean:
            if "MULTI-INSTRUMENT LOCK" in clean:
                status_update = "Looper: Bloqueo Voces 🔒"
            elif "RE-VOICE ALL" in clean:
                status_update = "Looper: Re-Voice Todo 🔀"
            else:
                match = re.search(r"RE-VOICE PISTAS\s+([0-9,\s]+)", clean)
                if match:
                    status_update = f"Looper: Re-Voice P{match.group(1).strip().replace(' ', '')} 🔀"
                elif "RE-VOICE PISTA ACTUAL" in clean:
                    status_update = "Looper: Re-Voice Última 🔀"
                else:
                    status_update = "Looper: Re-Voice Audición 🔀"
        elif "RESET DEFAULT COMPLETADO" in clean:
            match = re.search(r"1er Instrumento:\s*(.+?)(?:\s*\||\s*$)", clean)
            status_update = f"Reset: {match.group(1).strip()}" if match else "Reset: Factory Default"
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
            match = re.search(r"MODE \d+:\s*([^\]]+)", clean)
            if match:
                status_update = f"Mode: {match.group(1).strip()}"
        elif "DRUM KIT" in clean:
            match = re.search(r"->\s*(.+?)$", clean)
            if match:
                status_update = f"Drums: {match.group(1).strip()}"
        elif "METRONOME" in clean or "TAP TEMPO" in clean or "TEMPO E10" in clean or "NUDGE BPM" in clean:
            match = re.search(r"(\d+(?:\.\d+)?)\s*BPM", clean)
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
    is_knob = clean.startswith("[KNOB ")
    knob_id = clean.split("]")[0] if is_knob else None

    with _lock:
        coalesced = False
        if is_knob and len(_history[dev]) > 0:
            last_item = _history[dev][-1]
            if last_item.get("text", "").startswith(knob_id):
                last_item["text"] = clean
                last_item["time"] = timestamp
                entry = last_item
                coalesced = True
        if not coalesced:
            _history[dev].append(entry)
        if status_update:
            _status[dev] = status_update
        active_status = _status[dev]
        subs = list(_subscribers)

    payload = json.dumps({
        "device": dev,
        "entry": entry,
        "status": active_status,
        "coalesced": coalesced,
    })

    for client_queue in subs:
        try:
            client_queue.put_nowait(payload)
        except queue.Full:
            pass  # Drop if client queue is saturated to prevent blocking the reader


def _journal_listener_thread():
    """Background thread following systemd journal for the 3 controllers."""
    cmd = [
        "stdbuf", "-oL", "-eL",
        "journalctl",
        "-q",
        "-u", "synth.service",
        "-u", "apc40.service",
        "-u", "magicpad.service",
        "-f",
        "-n", "100",
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

            while True:
                raw_line = proc.stdout.readline()
                if not raw_line:
                    break
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
            # Fallback if stdbuf is missing on non-Linux
            cmd = [
                "journalctl",
                "-q",
                "-u", "synth.service",
                "-u", "apc40.service",
                "-u", "magicpad.service",
                "-f",
                "-n", "100",
                "-o", "json",
            ]
            time.sleep(5)
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
                    "hostname": socket.gethostname(),
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


def _ensure_ssl_certs():
    """Ensure a self-signed TLS certificate exists for HTTPS LAN operation."""
    cert_dir = os.path.expanduser("~/.waveform-lab-certs")
    os.makedirs(cert_dir, exist_ok=True)
    cert_file = os.path.join(cert_dir, "cert.pem")
    key_file = os.path.join(cert_dir, "key.pem")
    if not (os.path.exists(cert_file) and os.path.exists(key_file)):
        cmd = [
            "openssl", "req", "-x509",
            "-newkey", "rsa:2048",
            "-keyout", key_file,
            "-out", cert_file,
            "-days", "3650",
            "-nodes",
            "-subj", "/CN=waveform-lab",
        ]
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert_file, key_file


def main():
    parser = argparse.ArgumentParser(description="Waveform Lab Web & MIDI Telemetry Server")
    parser.add_argument("--port", "-p", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
                        help=f"HTTP Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--ssl-port", type=int, default=int(os.environ.get("SSL_PORT", 8443)),
                        help="HTTPS Port to listen on (default: 8443, 0 to disable)")
    parser.add_argument("--no-ssl", action="store_true", help="Disable HTTPS server")
    args = parser.parse_args()

    # Start journalctl background follower
    t = threading.Thread(target=_journal_listener_thread, daemon=True)
    t.start()

    # 1. HTTP Server
    http_address = ("0.0.0.0", args.port)
    httpd = ThreadingHTTPServer(http_address, MidiStreamHandler)

    # 2. HTTPS Server (for secure microphone context on LAN IP)
    https_server = None
    if not args.no_ssl and args.ssl_port > 0:
        try:
            cert_file, key_file = _ensure_ssl_certs()
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
            https_address = ("0.0.0.0", args.ssl_port)
            https_server = ThreadingHTTPServer(https_address, MidiStreamHandler)
            https_server.socket = ssl_ctx.wrap_socket(https_server.socket, server_side=True)
            https_thread = threading.Thread(target=https_server.serve_forever, daemon=True)
            https_thread.start()
        except Exception as e:
            print(f"[!] HTTPS setup warning: {e} (running HTTP only)")

    print("==================================================================")
    print(" Waveform Lab - Audio Visualizer & Multi-Device MIDI Telemetry   ")
    print(f" HTTP Server:  http://0.0.0.0:{args.port}/ (or localhost:{args.port})")
    if https_server:
        print(f" HTTPS Server: https://0.0.0.0:{args.ssl_port}/ (Secure Context for LAN)")
    print(" Endpoints:    /api/midi/state, /api/midi/stream                 ")
    print("==================================================================", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()
        if https_server:
            https_server.server_close()


if __name__ == "__main__":
    main()

