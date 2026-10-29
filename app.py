import os
import io
import csv
import json
import math
import uuid
import time
import socket
import threading
import http.client
import sqlite3
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, Response

app = Flask(__name__)
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8081")
DATA_DIR = os.environ.get("DATA_DIR", "/gmapsdata")
DOCKER_SOCKET_PATH = "/var/run/docker.sock"

# Docker Engine client over UNIX socket
class DockerUnixSocket(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost")
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(DOCKER_SOCKET_PATH)

def docker_api_request(method, path, body=None):
    try:
        conn = DockerUnixSocket()
        body_bytes = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body_bytes, headers=headers)
        res = conn.getresponse()
        resp_data = res.read().decode("utf-8", errors="replace")
        return res.status, resp_data
    except Exception as e:
        return 500, json.dumps({"error": str(e)})

def get_host_data_dir():
    env_host_dir = os.environ.get("HOST_DATA_DIR")
    if env_host_dir:
        return env_host_dir
    try:
        status, resp_data = docker_api_request("GET", "/v1.44/containers/gmaps-ui/json")
        if status == 200:
            info = json.loads(resp_data)
            for m in info.get("Mounts", []):
                if m.get("Destination") == "/gmapsdata":
                    return m.get("Source")
    except Exception:
        pass
    return "/home/wally/Documenti/GitHub/maps-scraper/data"

def docker_action(container_name, action):
    """action: 'pause', 'unpause', or 'json'"""
    try:
        conn = DockerUnixSocket()
        if action == "json":
            conn.request("GET", f"/v1.44/containers/{container_name}/json")
            res = conn.getresponse()
            if res.status == 200:
                data = json.loads(res.read().decode())
                return {"status": "ok", "state": data.get("State", {})}
            return {"status": "error", "code": res.status}
        else:
            conn.request("POST", f"/v1.44/containers/{container_name}/{action}")
            res = conn.getresponse()
            return {"status": "ok" if res.status in (200, 204) else "error", "code": res.status}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def get_db_connection():
    db_path = os.path.join(DATA_DIR, "jobs.db")
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    return None

def get_file_stats(job_id):
    """Returns (file_name, file_size_str, places_count) for a job."""
    local_csv = os.path.join(DATA_DIR, f"{job_id}.csv")
    if not os.path.exists(local_csv):
        return None, "—", 0
    try:
        sz = os.path.getsize(local_csv)
        if sz < 1024:
            sz_str = f"{sz} B"
        elif sz < 1024 * 1024:
            sz_str = f"{round(sz / 1024, 1)} KB"
        else:
            sz_str = f"{round(sz / (1024 * 1024), 2)} MB"
        
        count = 0
        with open(local_csv, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header:
                for _ in reader:
                    count += 1
        return f"{job_id}.csv", sz_str, count
    except Exception:
        return f"{job_id}.csv", "—", 0

def deduplicate_and_merge_csv(target_csv_path, source_csv_content):
    """
    Appends new rows from source_csv_content to target_csv_path,
    skipping rows that have already been recorded based on link, place_id, or title+address.
    """
    existing_identities = set()
    file_exists = os.path.exists(target_csv_path) and os.path.getsize(target_csv_path) > 0
    existing_fieldnames = []

    if file_exists:
        try:
            with open(target_csv_path, mode="r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                existing_fieldnames = reader.fieldnames or []
                for r in reader:
                    link = (r.get("link") or "").strip()
                    place_id = (r.get("place_id") or "").strip()
                    title = (r.get("title") or "").strip().lower()
                    addr = (r.get("address") or "").strip().lower()
                    if link: existing_identities.add(link)
                    if place_id: existing_identities.add(place_id)
                    if title and addr: existing_identities.add(f"{title}@@{addr}")
        except Exception as e:
            print(f"Error reading existing CSV: {e}")

    new_reader = csv.DictReader(io.StringIO(source_csv_content))
    new_fieldnames = new_reader.fieldnames or []
    
    # Combined ordered fieldnames
    fieldnames = existing_fieldnames[:] if existing_fieldnames else []
    for fn in new_fieldnames:
        if fn not in fieldnames:
            fieldnames.append(fn)

    rows_to_append = []
    for r in new_reader:
        link = (r.get("link") or "").strip()
        place_id = (r.get("place_id") or "").strip()
        title = (r.get("title") or "").strip().lower()
        addr = (r.get("address") or "").strip().lower()

        is_duplicate = False
        if link and link in existing_identities: is_duplicate = True
        elif place_id and place_id in existing_identities: is_duplicate = True
        elif (title and addr) and f"{title}@@{addr}" in existing_identities: is_duplicate = True

        if not is_duplicate:
            rows_to_append.append(r)
            if link: existing_identities.add(link)
            if place_id: existing_identities.add(place_id)
            if title and addr: existing_identities.add(f"{title}@@{addr}")

    mode = "a" if file_exists else "w"
    with open(target_csv_path, mode=mode, encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for r in rows_to_append:
            writer.writerow(r)

    return len(rows_to_append)

def monitor_active_jobs_limits():
    """
    Background worker that checks active jobs and auto-stops them
    when max_places limit is reached.
    """
    while True:
        try:
            time.sleep(3)
            conn = get_db_connection()
            if not conn:
                continue
            cur = conn.cursor()
            cur.execute("SELECT id, status, data FROM jobs WHERE status = 'working'")
            active_jobs = cur.fetchall()
            for r in active_jobs:
                jid = r["id"]
                d = json.loads(r["data"]) if r["data"] else {}
                max_places = d.get("max_places")
                if max_places and int(max_places) > 0:
                    limit_val = int(max_places)
                    _, _, count = get_file_stats(jid)
                    if count >= limit_val:
                        # Limit reached: stop runner & mark ok
                        stop_job_runner(jid)
                        cur.execute("UPDATE jobs SET status = 'ok', updated_at = ? WHERE id = ?", (int(time.time()), jid))
                        conn.commit()
            conn.close()
        except Exception:
            pass

# Start the background limit monitor daemon
threading.Thread(target=monitor_active_jobs_limits, daemon=True).start()

def stop_job_runner(job_id):
    """Stops and removes the dedicated CLI runner container for this job."""
    container_name = f"gmaps-runner-{job_id}"
    docker_api_request("POST", f"/v1.44/containers/{container_name}/stop?t=3")
    docker_api_request("DELETE", f"/v1.44/containers/{container_name}?force=true")

def monitor_job_runner(job_id, container_id):
    """Monitors the runner container until it finishes or is paused, then updates SQLite status."""
    while True:
        time.sleep(2)
        status_code, resp = docker_api_request("GET", f"/v1.44/containers/{container_id}/json")
        if status_code != 200:
            break
        try:
            info = json.loads(resp)
            state = info.get("State", {})
            if not state.get("Running", False):
                exit_code = state.get("ExitCode", 0)
                # Cleanup container
                docker_api_request("DELETE", f"/v1.44/containers/{container_id}?force=true")
                conn = get_db_connection()
                if conn:
                    cur = conn.cursor()
                    cur.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
                    row = cur.fetchone()
                    # Only mark as 'ok' if it wasn't deliberately marked as 'paused'
                    if row and row["status"] != "paused":
                        final_status = "ok" if exit_code == 0 else "failed"
                        cur.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                                    (final_status, int(time.time()), job_id))
                        conn.commit()
                    conn.close()
                break
        except Exception as e:
            print(f"Monitor error: {e}")
            break

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Google Maps Scraper</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css" />
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.css" />
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.css" />
    <style>
:root {
    --color-background: #111419;
    --color-surface: #171b22;
    --color-surface-hover: #1e232c;
    --color-border: #262c37;
    --color-border-hover: #363e4d;
    --color-text: #eef2f6;
    --color-text-light: #9ba7b7;
    --color-text-muted: #626e80;
    --color-primary: #0ea5e9;
    --color-primary-light: #38bdf8;
    --color-accent: #f59e0b;
    --color-success: #10b981;
    --color-warning: #f59e0b;
    --color-error: #f43f5e;
}

* { box-sizing: border-box; }

body {
    font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
    color: var(--color-text);
    background-color: var(--color-background);
    margin: 0;
    padding: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    font-size: 13.5px;
    letter-spacing: -0.01em;
}

.font-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }

.app-container {
    flex: 1;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
}

/* Top Navbar */
.top-navbar {
    height: 56px;
    background-color: var(--color-surface);
    border-bottom: 1px solid var(--color-border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    z-index: 100;
}

.top-navbar .navbar-brand {
    display: flex;
    align-items: center;
    gap: 10px;
}

.top-navbar .brand-icon {
    font-size: 1.35rem;
}

.brand-title {
    font-family: 'Space Grotesk', -apple-system, sans-serif;
    font-size: 1.15rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--color-text);
}

.top-navbar-right {
    display: flex;
    align-items: center;
    gap: 14px;
}

.nav-tabs {
    display: flex;
    align-items: center;
    gap: 4px;
    background-color: #0f1216;
    padding: 3px;
    border-radius: 8px;
    border: 1px solid var(--color-border);
}

.nav-tab-btn {
    padding: 6px 14px;
    font-size: 13px;
    font-weight: 600;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    background: transparent;
    color: var(--color-text-light);
    display: flex;
    align-items: center;
    gap: 6px;
    transition: all 0.15s ease-in-out;
}

.nav-tab-btn:hover {
    color: var(--color-text);
    background-color: rgba(255, 255, 255, 0.04);
}

.nav-tab-btn.active {
    background-color: var(--color-primary);
    color: #ffffff;
}

.tab-badge {
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 999px;
    background: rgba(0, 0, 0, 0.3);
    color: inherit;
    font-family: ui-monospace, monospace;
}

.view-section {
    display: none;
    flex: 1;
    overflow-y: auto;
    height: 100%;
}

.view-section.active {
    display: flex;
    flex-direction: column;
}

/* Fullscreen Search / Parameters Mode */
.search-fullscreen-container {
    max-width: 860px;
    margin: 0 auto;
    padding: 32px 24px;
    width: 100%;
}

.search-header-box {
    margin-bottom: 24px;
    text-align: left;
}

.search-header-box h2 {
    margin: 0 0 6px 0;
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--color-text);
}

.search-header-box p {
    margin: 0;
    color: var(--color-text-light);
    font-size: 13.5px;
}

/* Fullscreen Results Mode */
.results-fullscreen-container {
    padding: 24px 32px;
    width: 100%;
    box-sizing: border-box;
}

.empty-results-box {
    text-align: center;
    padding: 60px 20px;
    background: var(--color-surface);
    border: 1px dashed var(--color-border);
    border-radius: 8px;
    margin-top: 20px;
}

.empty-results-box .empty-icon {
    font-size: 42px;
    margin-bottom: 12px;
}

.empty-results-box h3 {
    margin: 0 0 6px 0;
    font-size: 1.2rem;
    color: var(--color-text);
}

.empty-results-box p {
    margin: 0 0 20px 0;
    color: var(--color-text-light);
    font-size: 13.5px;
}

main {
    flex: 1;
    display: flex;
    overflow: hidden;
}

.table-header-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
}

.table-title {
    font-family: 'Space Grotesk', -apple-system, sans-serif;
    font-size: 1.15rem;
    font-weight: 700;
    color: var(--color-text);
    display: flex;
    align-items: center;
    gap: 8px;
}

.engine-controls {
    display: flex;
    align-items: center;
    gap: 8px;
}

.btn-engine {
    padding: 6px 13px;
    border-radius: 6px;
    font-size: 12.5px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: all 0.15s;
}

.btn-engine-pause {
    background-color: rgba(194, 157, 98, 0.15);
    color: #c29d62;
    border-color: rgba(194, 157, 98, 0.3);
}
.btn-engine-pause:hover { background-color: rgba(194, 157, 98, 0.25); }

.btn-engine-resume {
    background-color: rgba(110, 148, 124, 0.15);
    color: #6e947c;
    border-color: rgba(110, 148, 124, 0.3);
}
.btn-engine-resume:hover { background-color: rgba(110, 148, 124, 0.25); }

form {
    display: flex;
    flex-direction: column;
    gap: 16px;
}

fieldset {
    border: 1px solid var(--color-border);
    background: var(--color-surface);
    border-radius: 8px;
    padding: 18px 20px;
    margin: 0;
}

legend {
    font-weight: 600;
    margin-bottom: 6px;
    color: var(--color-text);
    font-size: 14px;
    padding: 0 6px;
}

.form-group {
    margin-bottom: 14px;
}

label {
    display: block;
    margin-bottom: 5px;
    color: var(--color-text-light);
    font-size: 12.5px;
    font-weight: 500;
}

input[type="text"],
input[type="number"],
textarea,
select {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    font-size: 13px;
    box-sizing: border-box;
    background: #0f1216;
    color: var(--color-text);
    transition: border-color 0.15s;
}

input:focus, textarea:focus, select:focus {
    outline: none;
    border-color: var(--color-primary-light);
}

input[type="file"] {
    font-size: 12px;
    color: var(--color-text-light);
}

button {
    background-color: var(--color-primary);
    color: white;
    border: none;
    padding: 9px 18px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13.5px;
    font-weight: 600;
    transition: background-color 0.15s, opacity 0.15s;
}

button:hover { background-color: #0369a1; }
button:active { opacity: 0.9; }
button:disabled { background-color: #242c37; color: #64748b; cursor: not-allowed; }

/* Table Styling */
#table-wrapper {
    width: 100%;
    overflow-x: auto;
    border: 1px solid var(--color-border);
    border-radius: 8px;
    background-color: var(--color-surface);
}

table {
    width: 100%;
    min-width: 1060px;
    border-collapse: collapse;
    background-color: transparent;
    border: none;
}

th, td {
    text-align: left;
    padding: 9px 10px;
    border-bottom: 1px solid var(--color-border);
    vertical-align: middle;
}

th {
    font-weight: 700;
    color: var(--color-text-light);
    background-color: #13171d;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    white-space: nowrap;
}

tr:hover td { background-color: var(--color-surface-hover); }

.status-indicator {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    text-transform: lowercase;
    white-space: nowrap;
}

.status-ok { background-color: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); }
.status-pending { background-color: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.35); }
.status-working { background-color: rgba(14, 165, 233, 0.15); color: #38bdf8; border: 1px solid rgba(14, 165, 233, 0.35); }
.status-paused { background-color: rgba(245, 158, 11, 0.2); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.4); }
.status-failed, .status-error { background-color: rgba(244, 63, 94, 0.15); color: #f87171; border: 1px solid rgba(244, 63, 94, 0.35); }

/* Unified Action Buttons in Job Table */
.download-button, .delete-button, .view-button, .resume-button, .pause-job-button, .leads-button, .report-button, .json-button {
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-decoration: none;
    color: var(--color-text);
    background-color: #222834;
    border: 1px solid var(--color-border);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    margin: 0;
    white-space: nowrap;
    transition: all 0.12s ease-in-out;
}

.view-button {
    background-color: #0284c7;
    color: #ffffff;
    border-color: #0369a1;
}
.view-button:hover { background-color: #0369a1; }

.report-button {
    background-color: rgba(14, 165, 233, 0.12);
    color: #38bdf8;
    border-color: rgba(14, 165, 233, 0.35);
}
.report-button:hover { background-color: rgba(14, 165, 233, 0.25); color: #fff; }

.json-button {
    background-color: #1f2530;
    color: var(--color-text-light);
}
.json-button:hover {
    background-color: #2b3342;
    color: #fff;
}

.leads-button {
    background-color: rgba(245, 158, 11, 0.12);
    color: #fbbf24;
    border-color: rgba(245, 158, 11, 0.35);
}
.leads-button:hover {
    background-color: rgba(245, 158, 11, 0.22);
    color: #fde68a;
}

.delete-button {
    background-color: transparent;
    color: #f87171;
    border-color: rgba(244, 63, 94, 0.25);
}
.delete-button:hover {
    background-color: rgba(244, 63, 94, 0.18);
    color: #fda4af;
    border-color: rgba(244, 63, 94, 0.45);
}

.pause-job-button {
    background-color: rgba(245, 158, 11, 0.15);
    color: #fbbf24;
    border-color: rgba(245, 158, 11, 0.35);
    padding: 4px 7px;
}
.resume-button {
    background-color: rgba(16, 185, 129, 0.15);
    color: #34d399;
    border-color: rgba(16, 185, 129, 0.35);
    padding: 4px 7px;
}

.error-message {
    display: none;
    background-color: rgba(244, 63, 94, 0.12);
    border: 1px solid rgba(244, 63, 94, 0.3);
    color: #fca5a5;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 16px;
    font-size: 13px;
}
.error-message:not(:empty) { display: block; }

.expandable-section {
    border-radius: 6px;
    overflow: hidden;
}

.expandable-section summary {
    cursor: pointer;
    padding: 10px 14px;
    background-color: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 6px;
    color: var(--color-text);
    list-style: none;
    font-size: 13px;
    font-weight: 600;
    transition: background-color 0.15s;
}

.expandable-section summary:hover {
    background-color: var(--color-surface-hover);
}

.expandable-section summary::-webkit-details-marker {
    display: none;
}

.expandable-section[open] summary {
    border-bottom: none;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    background-color: var(--color-surface-hover);
}

.expandable-section[open] fieldset {
    border: 1px solid var(--color-border);
    border-top: none;
    border-radius: 0 0 6px 6px;
    padding: 14px;
    background: #12161c;
}

.spinner {
    display: none;
    width: 20px;
    height: 20px;
    border: 2px solid #242c37;
    border-top-color: var(--color-primary-light);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 12px;
}
.spinner.active { display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Map modal */
.map-modal {
    display: flex;
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: rgba(10, 12, 16, 0.82);
    backdrop-filter: blur(6px);
    justify-content: center;
    align-items: center;
}

.map-modal-content {
    background: var(--color-surface);
    color: var(--color-text);
    width: 92vw;
    height: 88vh;
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--color-border);
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6);
}

.map-modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 18px;
    border-bottom: 1px solid var(--color-border);
    background: #14181f;
}

.map-modal-header h2 { margin: 0; font-size: 1.15rem; color: var(--color-text); font-weight: 700; }

.map-modal-close {
    background: none;
    border: none;
    font-size: 1.5rem;
    line-height: 1;
    cursor: pointer;
    color: var(--color-text-light);
    padding: 0;
}
.map-modal-close:hover { color: #fff; }

.map-modal-body { flex: 1; display: flex; min-height: 0; }

#job-map { flex: 3; height: 100%; }

#job-places-list {
    flex: 1;
    min-width: 280px;
    max-width: 400px;
    overflow-y: auto;
    border-left: 1px solid var(--color-border);
    padding: 10px;
    background: var(--color-surface);
}

.place-item {
    padding: 10px 12px;
    border-bottom: 1px solid var(--color-border);
    cursor: pointer;
    border-radius: 6px;
    margin-bottom: 4px;
    background: #14181f;
    transition: background-color 0.15s;
}
.place-item:hover { background: #1c222b; }
.place-title { font-weight: 600; font-size: 13px; color: var(--color-text); }
.place-address { font-size: 12px; color: var(--color-text-light); margin-top: 2px; }
.place-rating { font-size: 12px; color: #fbbf24; font-weight: 600; margin-top: 2px; }

.map-loading, .map-empty, .map-error { padding: 12px; color: var(--color-text-light); font-size: 12.5px; }
.map-error { color: #f43f5e; }

.form-group.checkbox {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin-bottom: 12px;
}
.form-group.checkbox label { margin-bottom: 0; cursor: pointer; color: var(--color-text); }
.form-group.checkbox input[type="checkbox"] {
    width: 16px;
    height: 16px;
    margin-top: 2px;
    cursor: pointer;
    accent-color: var(--color-primary);
}

.badge-tag {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 3px;
    font-weight: 700;
    margin-left: 4px;
}
.badge-flag { background: #242c37; color: #94a3b8; }

.text-muted {
    font-size: 12px;
    color: var(--color-text-muted);
    line-height: 1.4;
    margin-top: 4px;
}

.actions-cell {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-wrap: nowrap;
    white-space: nowrap;
}

.grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
}

/* Pro Mode Switch Styling */
.pro-toggle-switch {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: #14181f;
    border: 1px solid var(--color-border);
    padding: 5px 10px;
    border-radius: 20px;
    cursor: pointer;
    user-select: none;
    transition: all 0.15s ease;
}
.pro-toggle-switch:hover { border-color: var(--color-primary-light); }
.pro-toggle-switch input[type="checkbox"] {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
    pointer-events: none;
}
.pro-slider {
    position: relative;
    width: 32px;
    height: 18px;
    background-color: #242c37;
    border-radius: 20px;
    transition: 0.2s;
    display: inline-block;
}
.pro-slider:before {
    position: absolute;
    content: "";
    height: 12px;
    width: 12px;
    left: 3px;
    bottom: 3px;
    background-color: #94a3b8;
    border-radius: 50%;
    transition: 0.2s;
}
.pro-toggle-switch input:checked + .pro-slider {
    background-color: var(--color-primary);
}
.pro-toggle-switch input:checked + .pro-slider:before {
    transform: translateX(14px);
    background-color: #ffffff;
}
.pro-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--color-text-light);
}
.pro-toggle-switch input:checked ~ .pro-label {
    color: var(--color-primary-light);
}

body:not(.pro-mode-active) .pro-field {
    display: none !important;
}
</style>
</head>
<body>
    <div class="app-container">
        <header class="top-navbar">
            <div class="navbar-brand">
                <span class="brand-icon">🗺️</span>
                <span class="brand-title">Google Maps Scraper</span>
            </div>

            <!-- View Navigation Switcher -->
            <div class="nav-tabs">
                <button id="tab-search-btn" class="nav-tab-btn active" onclick="switchView('search')" title="Imposta i parametri e fai una nuova ricerca">
                    🔍 Fai una ricerca
                </button>
                <button id="tab-results-btn" class="nav-tab-btn" onclick="switchView('results')" title="Visualizza i risultati e i job salvati">
                    📋 Risultati Salvati <span id="results-count-badge" class="tab-badge">0</span>
                </button>
            </div>

            <div class="top-navbar-right">
                <button id="engine-toggle-btn" class="btn-engine btn-engine-pause" onclick="toggleEnginePause()" title="Pause or resume all background scraping jobs">
                    ⏸
                </button>
            </div>
        </header>
        <main>
            <!-- View 1: Fai una ricerca (Parametri dello Scraper) -->
            <section id="view-search" class="view-section active">
                <div class="search-fullscreen-container">
                    <div class="search-header-box" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;">
                        <div>
                            <h2>Nuova Ricerca</h2>
                            <p>Configura i parametri del crawler ed estrai aziende, contatti e dati da Google Maps.</p>
                        </div>
                        <div style="display:flex; align-items:center; gap:12px;">
                            <label class="pro-toggle-switch" title="Attiva la modalità Pro per mostrare tutti i parametri avanzati">
                                <input type="checkbox" id="pro-mode-toggle" onchange="toggleProMode(this.checked)">
                                <span class="pro-slider"></span>
                                <span class="pro-label">⚡ Pro Mode</span>
                            </label>
                            <button type="button" class="btn-secondary" onclick="switchView('results')" style="background:transparent; border:1px solid var(--color-border); color:var(--color-primary-light); padding:8px 14px; border-radius:6px; cursor:pointer; font-weight:500; font-size:13.5px;">
                                📂 Vedi Risultati Salvati &rarr;
                            </button>
                        </div>
                    </div>

                    <div id="error-container" class="error-message"></div>

                    <form id="scrape-form" onsubmit="handleSubmit(event)">
                        <fieldset>
                            <legend>Job Details</legend>
                            <div class="form-group">
                                <label for="name">Job Name:</label>
                                <input type="text" id="name" name="name" value="Ristoranti Veronesi" placeholder="es. Ristoranti Milano" autofocus>
                            </div>
                            <div class="form-group">
                                <label for="keywords">Keywords / Queries: (one per line)</label>
                                <textarea id="keywords" name="keywords" rows="6" placeholder="ristoranti verona centro&#10;pizzerie verona">ristorante
pizzeria
bar
pub
gelateria
cucina</textarea>
                            </div>
                            <div class="form-group pro-field">
                                <label for="input_file">Or upload Queries Input File: <span class="badge-tag badge-flag">-input</span></label>
                                <input type="file" id="input_file" accept=".txt,.csv" onchange="handleInputFileUpload(event)">
                                <p class="text-muted"><small>Load search queries directly from a local text file.</small></p>
                            </div>
                            <div class="form-group pro-field">
                                <label for="lang">Language: <span class="badge-tag badge-flag">-lang</span></label>
                                <input type="text" id="lang" name="lang" value="it">
                            </div>
                        </fieldset>

                        <!-- Location Settings & Geo / Grid -->
                        <details class="expandable-section pro-field">
                            <summary>Location, Geo Coordinates & Grid Scrape</summary>
                            <fieldset>
                                <div class="form-group">
                                    <label for="geo">Geo Coordinates (Lat,Lon): <span class="badge-tag badge-flag">-geo</span></label>
                                    <input type="text" id="geo" value="45.3274,11.078924" placeholder="e.g. 45.4415,10.9972" onchange="parseGeoInput(this.value)">
                                    <p class="text-muted"><small>Format: latitude,longitude</small></p>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="latitude">Latitude:</label>
                                        <input type="number" step="0.000000000000001" id="latitude" name="latitude" value="45.3274">
                                    </div>
                                    <div class="form-group">
                                        <label for="longitude">Longitude:</label>
                                        <input type="number" step="0.000000000000001" id="longitude" name="longitude" value="11.078924">
                                    </div>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="zoom">Zoom (0-21): <span class="badge-tag badge-flag">-zoom</span></label>
                                        <input type="number" id="zoom" name="zoom" value="13" min="0" max="21" oninput="updateZoomAreaEstimate()">
                                        <p class="text-muted" id="zoom-estimate" style="color:#60a5fa; font-weight:500; font-size:12px; margin-top:4px;">
                                            📐 Superficie coperta: ~15 - 40 km² (Città media / Settore urbano)
                                        </p>
                                    </div>
                                    <div class="form-group">
                                        <label for="radius">Radius (meters): <span class="badge-tag badge-flag">-radius</span></label>
                                        <input type="number" id="radius" name="radius" value="20000">
                                    </div>
                                </div>

                                <hr style="border:none; border-top:1px solid var(--color-border); margin:14px 0;">

                                <div class="form-group">
                                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                                        <label for="grid_bbox" style="margin-bottom:0; font-weight:600;">Grid Bounding Box (4 punti): <span class="badge-tag badge-flag">-grid-bbox</span></label>
                                        <span id="grid-cell-count" style="font-size:11.5px; font-weight:600; color:#34d399;"></span>
                                    </div>
                                    <input type="text" id="grid_bbox" placeholder="minLat,minLon,maxLat,maxLon (es. 45.40,10.90,45.50,11.00)" oninput="drawGridPreview()">
                                    
                                    <!-- 4 Coordinate Points Explicit Inputs -->
                                    <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:8px; margin-top:8px;">
                                        <div>
                                            <label style="font-size:11px; margin-bottom:2px;">1. Min Lat (Sud):</label>
                                            <input type="number" step="0.0001" id="bbox_min_lat" placeholder="45.40" oninput="syncFromFourPoints()">
                                        </div>
                                        <div>
                                            <label style="font-size:11px; margin-bottom:2px;">2. Min Lon (Ovest):</label>
                                            <input type="number" step="0.0001" id="bbox_min_lon" placeholder="10.90" oninput="syncFromFourPoints()">
                                        </div>
                                        <div>
                                            <label style="font-size:11px; margin-bottom:2px;">3. Max Lat (Nord):</label>
                                            <input type="number" step="0.0001" id="bbox_max_lat" placeholder="45.50" oninput="syncFromFourPoints()">
                                        </div>
                                        <div>
                                            <label style="font-size:11px; margin-bottom:2px;">4. Max Lon (Est):</label>
                                            <input type="number" step="0.0001" id="bbox_max_lon" placeholder="11.00" oninput="syncFromFourPoints()">
                                        </div>
                                    </div>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="grid_cell">Dimensione Cella (km): <span class="badge-tag badge-flag">-grid-cell</span></label>
                                        <input type="number" id="grid_cell" value="1.0" step="0.1" min="0.1" oninput="updateGridFromCellKm()">
                                        <p class="text-muted"><small>Passo reticolo in km</small></p>
                                    </div>
                                    <div class="form-group">
                                        <label for="grid_cell_cols">Numero Celle Personalizzato (Righe × Colonne):</label>
                                        <div style="display:flex; gap:6px; align-items:center;">
                                            <input type="number" id="grid_cell_rows" value="3" min="1" max="100" placeholder="Righe (Lat)" oninput="updateGridFromRowsCols()">
                                            <span style="color:var(--color-text-light);">×</span>
                                            <input type="number" id="grid_cell_cols" value="3" min="1" max="100" placeholder="Colonne (Lon)" oninput="updateGridFromRowsCols()">
                                        </div>
                                        <p class="text-muted"><small>Calcola automaticamente la dimensione in km</small></p>
                                    </div>
                                </div>

                                <!-- Interactive Grid Preview Map -->
                                <div class="form-group">
                                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                                        <label style="margin-bottom:0; font-weight:600; color:var(--color-primary-light);">🗺️ Mappa Interattiva: 4 Punti & Celle</label>
                                        <span style="font-size:11px; color:var(--color-text-light);">Trascina i 4 angoli per ridimensionare il box</span>
                                    </div>
                                    <div id="grid-preview-map" style="height: 320px; width:100%; border-radius: 8px; border: 1px solid var(--color-border); background: #0f172a; position:relative;"></div>
                                    <div style="display:flex; justify-content:space-between; align-items:center; margin-top:6px; gap:8px; flex-wrap:wrap;">
                                        <small style="color:var(--color-text-light);">Trascina i marker d'angolo sulla mappa per regolare la Bounding Box, o clicca per centrarla.</small>
                                        <button type="button" class="btn-secondary" onclick="fitGridBounds()" style="padding:4px 10px; font-size:11px; border-radius:4px; border:1px solid var(--color-border); background:transparent; color:var(--color-text-light); cursor:pointer;">🔍 Inquadra Box</button>
                                    </div>
                                </div>
                            </fieldset>
                        </details>

                        <!-- Advanced Options (email, extra reviews, depth, radius, fastmode) -->
                        <details class="expandable-section pro-field">
                            <summary>Advanced Options & Crawler Behavior</summary>
                            <fieldset>
                                <div class="form-group checkbox">
                                    <input type="checkbox" id="email" name="email" checked>
                                    <label for="email"><strong>Extract emails</strong> from websites <span class="badge-tag badge-flag">-email</span></label>
                                </div>
                                <div class="form-group checkbox">
                                    <input type="checkbox" id="extra_reviews" name="extra_reviews">
                                    <label for="extra_reviews"><strong>Collect extra reviews</strong> (~300 reviews) <span class="badge-tag badge-flag">-extra-reviews</span></label>
                                </div>
                                <div class="form-group checkbox">
                                    <input type="checkbox" id="fastmode" name="fastmode">
                                    <label for="fastmode">Fast Mode (reduced data) <span class="badge-tag badge-flag">-fast-mode</span></label>
                                </div>
                                <div class="form-group checkbox">
                                    <input type="checkbox" id="disable_page_reuse" name="disable_page_reuse">
                                    <label for="disable_page_reuse"><strong>Disable Page Reuse</strong> (safer memory) <span class="badge-tag badge-flag">-disable-page-reuse</span></label>
                                </div>
                                <div class="form-group checkbox">
                                    <input type="checkbox" id="debug_mode" name="debug_mode">
                                    <label for="debug_mode"><strong>Headful / Debug Mode</strong> (show browser window) <span class="badge-tag badge-flag">-debug</span></label>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="depth">Depth (scrolls): <span class="badge-tag badge-flag">-depth</span></label>
                                        <input type="number" step="1" id="depth" name="depth" value="100">
                                    </div>
                                    <div class="form-group">
                                        <label for="max_places">Limite Locali (Max Places):</label>
                                        <input type="number" step="1" min="1" id="max_places" name="max_places" placeholder="Nessun limite (es. 50)">
                                    </div>
                                    <div class="form-group">
                                        <label for="maxtime">Max job time:</label>
                                        <input type="text" id="maxtime" name="maxtime" value="60000s">
                                    </div>
                                </div>

                                <div class="form-group">
                                    <label for="exit_on_inactivity">Exit on inactivity timeout: <span class="badge-tag badge-flag">-exit-on-inactivity</span></label>
                                    <input type="text" id="exit_on_inactivity" name="exit_on_inactivity" value="5m" placeholder="e.g. 5m, 10m">
                                    <p class="text-muted"><small>Terminates search automatically if no new results appear within duration.</small></p>
                                </div>
                            </fieldset>
                        </details>

                        <!-- Performance & Concurrency Settings -->
                        <details class="expandable-section pro-field">
                            <summary>Performance & Browser Pool</summary>
                            <fieldset>
                                <div class="form-group">
                                    <label for="concurrency">Concurrency (-c):</label>
                                    <select id="concurrency">
                                        <option value="1">1 worker (default)</option>
                                        <option value="2">2 workers</option>
                                        <option value="4" selected>4 workers (recommended)</option>
                                        <option value="8">8 workers</option>
                                        <option value="16">16 workers</option>
                                    </select>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="pages_per_browser">Pages per browser:</label>
                                        <input type="number" id="pages_per_browser" value="4" min="1" max="16">
                                        <p class="text-muted"><small>-pages-per-browser</small></p>
                                    </div>
                                    <div class="form-group">
                                        <label for="browser_pool_size">Browser pool size:</label>
                                        <input type="number" id="browser_pool_size" value="2" min="0" max="32">
                                        <p class="text-muted"><small>-browser-pool-size (0=auto)</small></p>
                                    </div>
                                </div>
                            </fieldset>
                        </details>

                        <!-- Proxies (Manual list or file upload) -->
                        <details class="expandable-section pro-field">
                            <summary>Proxies & Credentials File</summary>
                            <fieldset>
                                <div class="form-group">
                                    <label for="proxies">Proxies: (one per line, format: protocol://user:pass@host:port)</label>
                                    <textarea id="proxies" name="proxies" rows="3" placeholder="http://user:pass@host:8080&#10;socks5://host:1080"></textarea>
                                </div>

                                <div class="form-group">
                                    <label for="proxy_file">Upload proxies credentials file: <span class="badge-tag badge-flag">-proxies-file</span></label>
                                    <input type="file" id="proxy_file" accept=".txt,.csv" onchange="handleProxyFileUpload(event)">
                                    <p class="text-muted"><small>Load proxies directly from a local text file.</small></p>
                                </div>
                            </fieldset>
                        </details>

                        <!-- LeadsDB Integration -->
                        <details class="expandable-section pro-field">
                            <summary>Direct Export & Integrations</summary>
                            <fieldset>
                                <div class="form-group">
                                    <label for="leadsdb_api_key">LeadsDB API Key: <span class="badge-tag badge-flag">-leadsdb-api-key</span></label>
                                    <input type="text" id="leadsdb_api_key" placeholder="Enter key from getleadsdb.com">
                                    <p class="text-muted"><small>Saved in browser for one-click export from the results table.</small></p>
                                </div>

                                <hr style="border:none; border-top:1px solid var(--color-border); margin:14px 0;">

                                <div class="form-group">
                                    <label for="writer_plugin">Custom Writer Plugin: <span class="badge-tag badge-flag">-writer</span></label>
                                    <input type="text" id="writer_plugin" placeholder="e.g. dir:pluginName">
                                    <p class="text-muted"><small>Custom export writer plugin.</small></p>
                                </div>
                            </fieldset>
                        </details>

                        <!-- Cloud, Database & Distributed Scraping -->
                        <details class="expandable-section pro-field">
                            <summary>Database & Cloud Storage (Postgres / S3 / Lambda)</summary>
                            <fieldset>
                                <div class="form-group">
                                    <label for="dsn">PostgreSQL DSN: <span class="badge-tag badge-flag">-dsn</span></label>
                                    <input type="text" id="dsn" placeholder="postgres://user:password@host:5432/dbname">
                                    <p class="text-muted"><small>Connect to a PostgreSQL database provider.</small></p>
                                </div>
                                <div class="form-group checkbox">
                                    <input type="checkbox" id="produce" name="produce">
                                    <label for="produce"><strong>Produce Seed Jobs Only</strong> (requires -dsn) <span class="badge-tag badge-flag">-produce</span></label>
                                </div>

                                <hr style="border:none; border-top:1px solid var(--color-border); margin:14px 0;">

                                <div class="form-group">
                                    <label for="s3_bucket">Amazon S3 Bucket: <span class="badge-tag badge-flag">-s3-bucket</span></label>
                                    <input type="text" id="s3_bucket" placeholder="my-gmaps-results-bucket">
                                    <p class="text-muted"><small>Upload scrape results directly to Amazon S3.</small></p>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="aws_region">AWS Region: <span class="badge-tag badge-flag">-aws-region</span></label>
                                        <input type="text" id="aws_region" placeholder="us-east-1">
                                    </div>
                                    <div class="form-group">
                                        <label for="function_name">Lambda Function: <span class="badge-tag badge-flag">-function-name</span></label>
                                        <input type="text" id="function_name" placeholder="gmaps-worker-lambda">
                                    </div>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="aws_access_key">AWS Access Key: <span class="badge-tag badge-flag">-aws-access-key</span></label>
                                        <input type="password" id="aws_access_key" placeholder="AKIA...">
                                    </div>
                                    <div class="form-group">
                                        <label for="aws_secret_key">AWS Secret Key: <span class="badge-tag badge-flag">-aws-secret-key</span></label>
                                        <input type="password" id="aws_secret_key" placeholder="...">
                                    </div>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group checkbox">
                                        <input type="checkbox" id="aws_lambda" name="aws_lambda">
                                        <label for="aws_lambda">Run as Lambda <span class="badge-tag badge-flag">-aws-lambda</span></label>
                                    </div>
                                    <div class="form-group checkbox">
                                        <input type="checkbox" id="aws_lambda_invoker" name="aws_lambda_invoker">
                                        <label for="aws_lambda_invoker">Lambda Invoker <span class="badge-tag badge-flag">-aws-lambda-invoker</span></label>
                                    </div>
                                </div>

                                <div class="form-group">
                                    <label for="aws_lambda_chunk_size">Lambda Chunk Size: <span class="badge-tag badge-flag">-aws-lambda-chunk-size</span></label>
                                    <input type="number" id="aws_lambda_chunk_size" value="100" min="1">
                                </div>
                            </fieldset>
                        </details>

                        <button type="submit" id="submit-btn" style="margin-top:20px; font-size:15px; padding:12px;">🚀 Start Scraping</button>
                    </form>
                </div>
            </section>

            <!-- View 2: Visualizzazione Risultati Salvati (Jobs & Tasks) -->
            <section id="view-results" class="view-section">
                <div class="results-fullscreen-container">
                    <div class="table-header-bar">
                        <div class="table-title">
                            <span>Tasks & Scraping Jobs</span>
                        </div>
                        <div style="display:flex; gap:10px; align-items:center;">
                            <button type="button" onclick="openMergeModal()" style="background-color:var(--color-primary); color:#fff; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-weight:600; font-size:13.5px; display:inline-flex; align-items:center; gap:6px;">
                                🔀 Unisci Ricerche (Merge)
                            </button>
                            <button type="button" class="btn-primary" onclick="switchView('search')" style="background-color:var(--color-primary); color:#fff; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-weight:600; font-size:13.5px; display:inline-flex; align-items:center; gap:6px;">
                                ➕ Nuova Ricerca
                            </button>
                        </div>
                    </div>

                    <div id="spinner" class="spinner"></div>

                    <div id="empty-state-container" class="empty-results-box" style="display:none;">
                        <div class="empty-icon">📂</div>
                        <h3>Nessun lavoro o risultato salvato</h3>
                        <p>Non hai ancora effettuato ricerche. Avvia una ricerca inserendo le parole chiave.</p>
                        <button type="button" onclick="switchView('search')" style="background-color:var(--color-primary); color:#fff; border:none; padding:10px 20px; border-radius:6px; cursor:pointer; font-weight:600;">
                            🔍 Fai una ricerca adesso
                        </button>
                    </div>

                    <div id="table-wrapper">
                        <table id="job-table">
                            <thead>
                                <tr>
                                    <th style="width:105px;">Job ID</th>
                                    <th>Job Name</th>
                                    <th style="width:90px;text-align:center;">Locali</th>
                                    <th style="width:170px;">File Locale</th>
                                    <th style="width:160px;">Inizio / Fine</th>
                                    <th style="width:85px;text-align:center;">Status</th>
                                    <th style="width:230px;text-align:left;">Actions</th>
                                </tr>
                            </thead>
                            <tbody id="job-tbody">
                                <tr><td colspan="5" style="color:var(--color-text-light); padding:16px;">Loading jobs...</td></tr>
                            </tbody>
                        </table>
                    </div>
                    <div id="pagination-container"></div>
                </div>
            </section>
        </main>
    </div>

    <!-- Map Modal -->
    <div id="map-modal-container"></div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.js"></script>
    <script>
    // ---- Pause / Resume All Jobs Engine Control ----
    let isEnginePaused = false;

    async function checkEngineStatus() {
        try {
            const res = await fetch('/api/engine/status');
            const data = await res.json();
            updateEngineUI(data.paused);
        } catch (e) {
            console.error('Engine status check error:', e);
        }
    }

    function updateEngineUI(paused) {
        isEnginePaused = paused;
        const badge = document.getElementById('engine-status-badge');
        const btn = document.getElementById('engine-toggle-btn');
        if (badge) {
            badge.className = paused ? 'badge-engine badge-engine-paused' : 'badge-engine badge-engine-running';
            badge.textContent = paused ? 'Engine Paused' : 'Engine Active';
        }
        if (btn) {
            btn.className = paused ? 'btn-engine btn-engine-resume' : 'btn-engine btn-engine-pause';
            btn.innerHTML = paused ? '▶' : '⏸';
            btn.title = paused ? 'Resume All Jobs' : 'Pause All Jobs';
        }
    }

    async function toggleEnginePause() {
        const btn = document.getElementById('engine-toggle-btn');
        btn.disabled = true;
        try {
            const targetAction = isEnginePaused ? 'resume' : 'pause';
            const res = await fetch('/api/engine/' + targetAction, { method: 'POST' });
            const data = await res.json();
            updateEngineUI(data.paused);
        } catch (e) {
            alert('Error toggling engine state: ' + e.message);
        } finally {
            btn.disabled = false;
        }
    }

    // ---- Error display ----
    function showError(msg) {
        const el = document.getElementById('error-container');
        el.textContent = msg;
        el.style.display = msg ? 'block' : 'none';
    }

    // ---- View Switcher & Intelligent Routing ----
    let currentView = 'results';
    let hasLoadedInitialView = false;

    function switchView(viewName) {
        currentView = viewName;
        const searchView = document.getElementById('view-search');
        const resultsView = document.getElementById('view-results');
        const searchBtn = document.getElementById('tab-search-btn');
        const resultsBtn = document.getElementById('tab-results-btn');

        if (viewName === 'search') {
            searchView.classList.add('active');
            resultsView.classList.remove('active');
            searchBtn.classList.add('active');
            resultsBtn.classList.remove('active');
            setTimeout(() => {
                if (gridMap) gridMap.invalidateSize();
            }, 100);
        } else {
            resultsView.classList.add('active');
            searchView.classList.remove('active');
            resultsBtn.classList.add('active');
            searchBtn.classList.remove('active');
        }
    }

    // ---- Pro Mode Toggle ----
    function toggleProMode(isPro) {
        localStorage.setItem('pro_mode_enabled', isPro ? 'true' : 'false');
        const toggleEl = document.getElementById('pro-mode-toggle');
        if (toggleEl && toggleEl.checked !== isPro) {
            toggleEl.checked = isPro;
        }

        if (isPro) {
            document.body.classList.add('pro-mode-active');
            // When Pro Mode is enabled, expand all expandable sections by default (user can still collapse them)
            document.querySelectorAll('.expandable-section').forEach(el => {
                el.open = true;
            });
        } else {
            document.body.classList.remove('pro-mode-active');
        }
    }

    // ---- Zoom Area Estimation & Map Grid Visualizer ----
    const ZOOM_RANGES = {
        0:  { range: "500.000.000+ km²", desc: "Mondo intero" },
        1:  { range: "200.000.000 - 300.000.000 km²", desc: "Emisfero" },
        2:  { range: "50.000.000 - 100.000.000 km²", desc: "Continente esteso" },
        3:  { range: "15.000.000 - 30.000.000 km²", desc: "Subcontinente" },
        4:  { range: "4.000.000 - 8.000.000 km²", desc: "Grande Nazione (es. USA/Brasile)" },
        5:  { range: "1.000.000 - 2.500.000 km²", desc: "Nazione estesa" },
        6:  { range: "250.000 - 600.000 km²", desc: "Nazione media (es. Italia)" },
        7:  { range: "60.000 - 150.000 km²", desc: "Macro-regione" },
        8:  { range: "15.000 - 40.000 km²", desc: "Regione (es. Veneto/Lombardia)" },
        9:  { range: "4.000 - 10.000 km²", desc: "Provincia estesa" },
        10: { range: "1.000 - 2.500 km²", desc: "Area Metropolitana / Provincia" },
        11: { range: "250 - 600 km²", desc: "Grande Città e Hinterland" },
        12: { range: "60 - 160 km²", desc: "Intera Città (es. Milano, Verona)" },
        13: { range: "15 - 40 km²", desc: "Città media / Settore urbano" },
        14: { range: "4 - 10 km²", desc: "Distretto Urbano / Macro-quartiere" },
        15: { range: "1.2 - 3.5 km²", desc: "Quartiere / Centro Storico" },
        16: { range: "0.3 - 0.9 km² (~30-90 ettari)", desc: "Gruppo di isolati" },
        17: { range: "0.08 - 0.25 km² (~8-25 ettari)", desc: "Singolo isolato / Piazza" },
        18: { range: "0.02 - 0.06 km² (~2-6 ettari)", desc: "Via o complesso di edifici" },
        19: { range: "0.005 - 0.015 km² (~5.000-15.000 m²)", desc: "Singolo edificio / Cortile" },
        20: { range: "0.001 - 0.004 km² (~1.000-4.000 m²)", desc: "Dettaglio architettonico" },
        21: { range: "< 0.001 km² (< 1.000 m²)", desc: "Massimo ingrandimento" }
    };

    function updateZoomAreaEstimate() {
        const zoomInput = document.getElementById('zoom');
        const zoomEl = document.getElementById('zoom-estimate');
        if (!zoomInput || !zoomEl) return;
        const z = parseInt(zoomInput.value);
        const info = ZOOM_RANGES[z] || { range: "N/A", desc: "Personalizzato" };
        zoomEl.innerHTML = `📐 Superficie coperta: <strong>${info.range}</strong> <span style="opacity:0.8;">(${info.desc})</span>`;
    }

    let gridMap = null;
    let gridLayers = null;
    let cornerMarkers = [];
    let isInternalUpdate = false;

    function syncFromFourPoints() {
        if (isInternalUpdate) return;
        const minLat = parseFloat(document.getElementById('bbox_min_lat')?.value);
        const minLon = parseFloat(document.getElementById('bbox_min_lon')?.value);
        const maxLat = parseFloat(document.getElementById('bbox_max_lat')?.value);
        const maxLon = parseFloat(document.getElementById('bbox_max_lon')?.value);

        if (!isNaN(minLat) && !isNaN(minLon) && !isNaN(maxLat) && !isNaN(maxLon)) {
            document.getElementById('grid_bbox').value = `${minLat.toFixed(4)},${minLon.toFixed(4)},${maxLat.toFixed(4)},${maxLon.toFixed(4)}`;
            drawGridPreview();
        }
    }

    function updateGridFromRowsCols() {
        const rows = parseInt(document.getElementById('grid_cell_rows')?.value) || 3;
        const cols = parseInt(document.getElementById('grid_cell_cols')?.value) || 3;
        const bboxVal = document.getElementById('grid_bbox')?.value.trim();
        if (!bboxVal) return;

        const parts = bboxVal.split(',').map(s => parseFloat(s.trim()));
        if (parts.length === 4 && !parts.some(isNaN) && parts[0] < parts[2] && parts[1] < parts[3]) {
            const [minLat, minLon, maxLat, maxLon] = parts;
            const midLat = (minLat + maxLat) / 2;
            const totalLatKm = (maxLat - minLat) * 110.574;
            const totalLonKm = (maxLon - minLon) * (111.320 * Math.cos(midLat * Math.PI / 180));
            const calculatedKm = Math.max(0.1, (totalLatKm / rows + totalLonKm / cols) / 2);
            document.getElementById('grid_cell').value = calculatedKm.toFixed(2);
            drawGridPreview();
        }
    }

    function updateGridFromCellKm() {
        drawGridPreview();
    }

    function initGridMap() {
        if (gridMap) return;
        const container = document.getElementById('grid-preview-map');
        if (!container) return;

        let lat = parseFloat(document.getElementById('latitude')?.value) || 45.4384;
        let lon = parseFloat(document.getElementById('longitude')?.value) || 10.9916;
        if (lat === 0 && lon === 0) { lat = 45.4384; lon = 10.9916; }

        gridMap = L.map('grid-preview-map', {
            attributionControl: false
        }).setView([lat, lon], 12);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19
        }).addTo(gridMap);

        gridLayers = L.layerGroup().addTo(gridMap);

        // Click on map to set or move bounding box centered at click
        gridMap.on('click', (e) => {
            const clickLat = e.latlng.lat;
            const clickLon = e.latlng.lng;
            const cellSizeKm = parseFloat(document.getElementById('grid_cell')?.value) || 1.0;
            const rows = parseInt(document.getElementById('grid_cell_rows')?.value) || 3;
            const cols = parseInt(document.getElementById('grid_cell_cols')?.value) || 3;

            const halfLatKm = (cellSizeKm * rows) / 2;
            const halfLonKm = (cellSizeKm * cols) / 2;
            const dLat = halfLatKm / 110.574;
            const dLon = halfLonKm / (111.320 * Math.cos(clickLat * Math.PI / 180));

            const minLat = (clickLat - dLat).toFixed(4);
            const maxLat = (clickLat + dLat).toFixed(4);
            const minLon = (clickLon - dLon).toFixed(4);
            const maxLon = (clickLon + dLon).toFixed(4);

            document.getElementById('grid_bbox').value = `${minLat},${minLon},${maxLat},${maxLon}`;
            drawGridPreview();
        });

        drawGridPreview();
    }

    function drawGridPreview() {
        if (!gridMap || !gridLayers) return;
        gridLayers.clearLayers();
        cornerMarkers.forEach(m => gridMap.removeLayer(m));
        cornerMarkers = [];

        const bboxVal = document.getElementById('grid_bbox')?.value.trim();
        const countBadge = document.getElementById('grid-cell-count');
        const cellSizeKm = parseFloat(document.getElementById('grid_cell')?.value) || 1.0;

        if (!bboxVal) {
            if (countBadge) countBadge.textContent = '';
            return;
        }

        const parts = bboxVal.split(',').map(s => parseFloat(s.trim()));
        if (parts.length !== 4 || parts.some(isNaN)) {
            if (countBadge) countBadge.textContent = 'Formato non valido';
            return;
        }

        const [minLat, minLon, maxLat, maxLon] = parts;
        if (minLat >= maxLat || minLon >= maxLon) {
            if (countBadge) countBadge.textContent = 'Coordinate min/max invertite';
            return;
        }

        // Sync individual 4 coordinate inputs without trigger loop
        isInternalUpdate = true;
        if (document.getElementById('bbox_min_lat')) document.getElementById('bbox_min_lat').value = minLat.toFixed(4);
        if (document.getElementById('bbox_min_lon')) document.getElementById('bbox_min_lon').value = minLon.toFixed(4);
        if (document.getElementById('bbox_max_lat')) document.getElementById('bbox_max_lat').value = maxLat.toFixed(4);
        if (document.getElementById('bbox_max_lon')) document.getElementById('bbox_max_lon').value = maxLon.toFixed(4);
        isInternalUpdate = false;

        // Draw main bounding box rectangle
        const bounds = [[minLat, minLon], [maxLat, maxLon]];
        L.rectangle(bounds, {
            color: '#3b82f6',
            weight: 2,
            fillColor: '#3b82f6',
            fillOpacity: 0.08
        }).addTo(gridLayers);

        // Add 4 draggable corner markers to interactively resize the box
        const corners = [
            { id: 'sw', lat: minLat, lon: minLon, title: 'Sud-Ovest (Min Lat, Min Lon)' },
            { id: 'ne', lat: maxLat, lon: maxLon, title: 'Nord-Est (Max Lat, Max Lon)' },
            { id: 'nw', lat: maxLat, lon: minLon, title: 'Nord-Ovest (Max Lat, Min Lon)' },
            { id: 'se', lat: minLat, lon: maxLon, title: 'Sud-Est (Min Lat, Max Lon)' }
        ];

        corners.forEach(c => {
            const m = L.circleMarker([c.lat, c.lon], {
                radius: 7,
                fillColor: '#3b82f6',
                color: '#ffffff',
                weight: 2,
                fillOpacity: 1
            }).addTo(gridMap);
            m.bindTooltip(c.title, { direction: 'top', offset: [0, -6] });
            cornerMarkers.push(m);
        });

        // Calculate step sizes
        const midLat = (minLat + maxLat) / 2;
        const stepLat = cellSizeKm / 110.574;
        const stepLon = cellSizeKm / (111.320 * Math.cos(midLat * Math.PI / 180));

        let cellsCount = 0;
        const MAX_DRAWABLE_CELLS = 400; // Cap visual rendering to keep UI butter-smooth

        const latSteps = Math.max(1, Math.ceil((maxLat - minLat) / stepLat));
        const lonSteps = Math.max(1, Math.ceil((maxLon - minLon) / stepLon));
        const totalCalculatedCells = latSteps * lonSteps;

        if (countBadge) {
            countBadge.textContent = `Totale celle: ${totalCalculatedCells} (${latSteps}×${lonSteps} celle da ${cellSizeKm} km)`;
        }

        // Keep rows and cols input values in sync
        if (document.getElementById('grid_cell_rows') && document.activeElement !== document.getElementById('grid_cell_rows')) {
            document.getElementById('grid_cell_rows').value = latSteps;
        }
        if (document.getElementById('grid_cell_cols') && document.activeElement !== document.getElementById('grid_cell_cols')) {
            document.getElementById('grid_cell_cols').value = lonSteps;
        }

        // Draw grid lines/cells
        for (let lat = minLat; lat < maxLat; lat += stepLat) {
            for (let lon = minLon; lon < maxLon; lon += stepLon) {
                cellsCount++;
                if (cellsCount <= MAX_DRAWABLE_CELLS) {
                    const cMaxLat = Math.min(lat + stepLat, maxLat);
                    const cMaxLon = Math.min(lon + stepLon, maxLon);
                    L.rectangle([[lat, lon], [cMaxLat, cMaxLon]], {
                        color: '#10b981',
                        weight: 1,
                        dashArray: '3, 4',
                        fillColor: '#10b981',
                        fillOpacity: 0.12
                    }).addTo(gridLayers);
                }
            }
        }
    }

    function fitGridBounds() {
        if (!gridMap) return;
        const bboxVal = document.getElementById('grid_bbox')?.value.trim();
        if (!bboxVal) return;
        const parts = bboxVal.split(',').map(s => parseFloat(s.trim()));
        if (parts.length === 4 && !parts.some(isNaN) && parts[0] < parts[2] && parts[1] < parts[3]) {
            gridMap.fitBounds([[parts[0], parts[1]], [parts[2], parts[3]]], { padding: [30, 30] });
        }
    }

    // ---- LeadsDB Key Storage & Init Pro Mode ----
    window.addEventListener('DOMContentLoaded', () => {
        const savedKey = localStorage.getItem('leadsdb_api_key') || '';
        if (savedKey) {
            document.getElementById('leadsdb_api_key').value = savedKey;
        }
        document.getElementById('leadsdb_api_key').addEventListener('input', (e) => {
            localStorage.setItem('leadsdb_api_key', e.target.value.trim());
        });

        // Initialize Pro Mode (default is TRUE)
        const storedPro = localStorage.getItem('pro_mode_enabled');
        const isPro = storedPro === null ? true : storedPro === 'true';
        toggleProMode(isPro);

        updateZoomAreaEstimate();

        // Delay map init slightly so container geometry is computed
        setTimeout(() => {
            initGridMap();
        }, 200);

        checkEngineStatus();
    });

    // ---- Parse Geo string "lat,lon" ----
    function parseGeoInput(val) {
        if (!val) return;
        const parts = val.split(',').map(s => s.trim());
        if (parts.length >= 2) {
            const lat = parseFloat(parts[0]);
            const lon = parseFloat(parts[1]);
            if (!isNaN(lat) && !isNaN(lon)) {
                document.getElementById('latitude').value = lat;
                document.getElementById('longitude').value = lon;
            }
        }
    }

    // ---- Input File Upload Handler (-input) ----
    function handleInputFileUpload(event) {
        const file = event.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = function(e) {
            const content = e.target.result;
            const current = document.getElementById('keywords').value.trim();
            const newQueries = content.trim();
            if (current) {
                document.getElementById('keywords').value = current + '\\n' + newQueries;
            } else {
                document.getElementById('keywords').value = newQueries;
            }
        };
        reader.readAsText(file);
    }

    // ---- Proxy File Upload Handler (-proxies-file) ----
    function handleProxyFileUpload(event) {
        const file = event.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = function(e) {
            const content = e.target.result;
            const current = document.getElementById('proxies').value.trim();
            const newProxies = content.trim();
            if (current) {
                document.getElementById('proxies').value = current + '\\n' + newProxies;
            } else {
                document.getElementById('proxies').value = newProxies;
            }
        };
        reader.readAsText(file);
    }

    // ---- Individual Job Pause & Resume Actions ----
    async function pauseJob(jobId) {
        try {
            const res = await fetch('/api/v1/jobs/' + jobId + '/pause', { method: 'POST' });
            if (!res.ok) {
                const err = await res.text();
                alert('Pause error: ' + err);
            } else {
                fetchJobs();
            }
        } catch (e) {
            alert('Request error: ' + e.message);
        }
    }

    async function resumeJobSearch(jobId) {
        try {
            const res = await fetch('/api/v1/jobs/' + jobId + '/resume', { method: 'POST' });
            if (!res.ok) {
                const err = await res.text();
                alert('Resume error: ' + err);
            } else {
                const data = await res.json();
                fetchJobs();
            }
        } catch (e) {
            alert('Request error: ' + e.message);
        }
    }

    // ---- Export to LeadsDB Handler (-leadsdb-api-key) ----
    async function exportToLeadsDB(jobId) {
        const apiKey = document.getElementById('leadsdb_api_key').value.trim() || localStorage.getItem('leadsdb_api_key');
        if (!apiKey) {
            alert('Please enter your LeadsDB API Key under "Direct Export & Integrations" or get one at getleadsdb.com');
            return;
        }
        if (!confirm('Export this dataset to LeadsDB using your API Key?')) return;
        try {
            const res = await fetch('/api/export/leadsdb/' + jobId, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ api_key: apiKey })
            });
            const result = await res.json();
            if (res.ok) {
                alert('Successfully exported ' + (result.count || 0) + ' leads to LeadsDB!');
            } else {
                alert('Error exporting to LeadsDB: ' + (result.error || 'Server error'));
            }
        } catch (err) {
            alert('Request error: ' + err.message);
        }
    }

    // ---- Parse maxtime string (e.g. "10m", "1h", "90s") to seconds integer ----
    function parseMaxTime(str) {
        str = (str || '').trim().toLowerCase();
        const m = str.match(/^(\\d+(?:\\.\\d+)?)(h|m|s)?$/);
        if (!m) return 600;
        const n = parseFloat(m[1]);
        const unit = m[2] || 's';
        if (unit === 'h') return Math.round(n * 3600);
        if (unit === 'm') return Math.round(n * 60);
        return Math.round(n);
    }

    // ---- Form submit ----
    async function handleSubmit(e) {
        e.preventDefault();
        showError('');

        const btn = document.getElementById('submit-btn');
        const spinner = document.getElementById('spinner');
        btn.disabled = true;
        btn.textContent = 'Starting...';
        spinner.classList.add('active');

        const name = document.getElementById('name').value.trim();
        const rawKeywords = document.getElementById('keywords').value.trim();
        const lang = document.getElementById('lang').value.trim() || 'en';
        const zoom = parseInt(document.getElementById('zoom').value) || 15;
        const latitude = document.getElementById('latitude').value.trim();
        const longitude = document.getElementById('longitude').value.trim();
        const fastmode = document.getElementById('fastmode').checked;
        const radius = parseInt(document.getElementById('radius').value) || 10000;
        const depth = parseInt(document.getElementById('depth').value) || 10;
        const max_places_input = document.getElementById('max_places').value.trim();
        const max_places = max_places_input ? parseInt(max_places_input) : null;
        const email = document.getElementById('email').checked;
        const extra_reviews = document.getElementById('extra_reviews').checked;
        const disable_page_reuse = document.getElementById('disable_page_reuse').checked;
        const debug_mode = document.getElementById('debug_mode').checked;
        const exit_on_inactivity = document.getElementById('exit_on_inactivity').value.trim() || '5m';
        const maxtimeStr = document.getElementById('maxtime').value.trim() || '10m';
        const maxtime = parseMaxTime(maxtimeStr);
        const concurrency = parseInt(document.getElementById('concurrency').value) || 4;
        const pages_per_browser = parseInt(document.getElementById('pages_per_browser').value) || 4;
        const browser_pool_size = parseInt(document.getElementById('browser_pool_size').value) || 2;
        const grid_bbox = document.getElementById('grid_bbox').value.trim();
        const grid_cell = parseFloat(document.getElementById('grid_cell').value) || 1.0;
        const rawProxies = document.getElementById('proxies').value.trim();
        const writer_plugin = document.getElementById('writer_plugin').value.trim();
        const dsn = document.getElementById('dsn').value.trim();
        const produce = document.getElementById('produce').checked;
        const s3_bucket = document.getElementById('s3_bucket').value.trim();
        const aws_region = document.getElementById('aws_region').value.trim();
        const function_name = document.getElementById('function_name').value.trim();
        const aws_access_key = document.getElementById('aws_access_key').value.trim();
        const aws_secret_key = document.getElementById('aws_secret_key').value.trim();
        const aws_lambda = document.getElementById('aws_lambda').checked;
        const aws_lambda_invoker = document.getElementById('aws_lambda_invoker').checked;
        const aws_lambda_chunk_size = parseInt(document.getElementById('aws_lambda_chunk_size').value) || 100;

        if (!name) { showError('Job Name is required.'); btn.disabled = false; btn.textContent = 'Start Scraping'; spinner.classList.remove('active'); return; }
        const keywords = rawKeywords.split('\\n').map(s => s.trim()).filter(s => s.length > 0);
        if (keywords.length === 0) { showError('At least one keyword is required.'); btn.disabled = false; btn.textContent = 'Start Scraping'; spinner.classList.remove('active'); return; }

        if (fastmode && grid_bbox) {
            showError('Notice: -fast-mode cannot be used together with -grid-bbox.');
            btn.disabled = false; btn.textContent = 'Start Scraping'; spinner.classList.remove('active');
            return;
        }

        const proxies = rawProxies ? rawProxies.split('\\n').map(p => p.trim()).filter(p => p.length > 0) : [];

        const payload = {
            name,
            keywords,
            lang,
            zoom,
            lat: latitude || null,
            lon: longitude || null,
            fast_mode: fastmode,
            radius,
            depth,
            max_places,
            email,
            max_time: maxtime,
            extra_reviews,
            disable_page_reuse,
            debug: debug_mode,
            exit_on_inactivity,
            concurrency,
            pages_per_browser,
            browser_pool_size,
            grid_bbox: grid_bbox || null,
            grid_cell,
            proxies,
            writer: writer_plugin || null,
            dsn: dsn || null,
            produce,
            s3_bucket: s3_bucket || null,
            aws_region: aws_region || null,
            function_name: function_name || null,
            aws_access_key: aws_access_key || null,
            aws_secret_key: aws_secret_key || null,
            aws_lambda,
            aws_lambda_invoker,
            aws_lambda_chunk_size
        };

        try {
            const res = await fetch('/api/v1/jobs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!res.ok) {
                const text = await res.text();
                showError('Error: ' + text);
            } else {
                document.getElementById('name').value = '';
                document.getElementById('keywords').value = '';
                await fetchJobs();
                switchView('results');
            }
        } catch (err) {
            showError('Connection error: ' + err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Start Scraping';
            spinner.classList.remove('active');
        }
    }

    // ---- Jobs table ----
    let cachedJobs = [];
    function escapeHtml(str) {
        return String(str == null ? '' : str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    async function fetchJobs() {
        try {
            const res = await fetch('/api/v1/jobs');
            if (!res.ok) return;
            const jobs = await res.json();
            cachedJobs = jobs || [];

            // Update badge count
            const countBadge = document.getElementById('results-count-badge');
            if (countBadge) {
                countBadge.textContent = cachedJobs.length;
            }

            // Routing on first load:
            // "all'inizio se non ci sono risultati, apre 'Fai una ricerca' con solo i parametri.
            // poi se ci sono job o risultati salvati, apre direttamente la lista dei job"
            if (!hasLoadedInitialView) {
                hasLoadedInitialView = true;
                if (cachedJobs.length === 0) {
                    switchView('search');
                } else {
                    switchView('results');
                }
            }

            renderJobs(cachedJobs);
            checkEngineStatus();
        } catch (err) {
            console.error('fetchJobs error:', err);
        }
    }

    function renderJobs(jobs) {
        const tbody = document.getElementById('job-tbody');
        const emptyState = document.getElementById('empty-state-container');
        const tableWrapper = document.getElementById('table-wrapper');

        if (!jobs || jobs.length === 0) {
            if (emptyState) emptyState.style.display = 'block';
            if (tableWrapper) tableWrapper.style.display = 'none';
            tbody.innerHTML = '';
            return;
        }

        if (emptyState) emptyState.style.display = 'none';
        if (tableWrapper) tableWrapper.style.display = 'block';

        let html = '';
        for (const j of jobs) {
            const id = escapeHtml(j.id || '');
            const name = escapeHtml(j.name || '—');
            const date = j.date ? new Date(j.date).toLocaleString() : '—';
            const status = j.status || 'pending';

            let statusHtml = '';
            if (status === 'ok') {
                statusHtml = '<span class="status-indicator status-ok">ok</span>';
            } else if (status === 'working') {
                statusHtml = '<span class="status-indicator status-working">working</span>';
            } else if (status === 'pending') {
                statusHtml = '<span class="status-indicator status-pending">pending</span>';
            } else if (status === 'paused') {
                statusHtml = '<span class="status-indicator status-paused">paused ⏸</span>';
            } else if (status === 'failed' || status === 'error') {
                statusHtml = '<span class="status-indicator status-failed">' + escapeHtml(status) + '</span>';
            } else {
                statusHtml = '<span class="status-indicator">' + escapeHtml(status) + '</span>';
            }

            let actionsHtml = '<div class="actions-cell">';

            // Pause button for currently working jobs
            if (status === 'working') {
                actionsHtml += `<button class="pause-job-button" onclick="pauseJob('${j.id}')" title="Pause this search and save results">⏸</button>`;
            }

            // Resume button for paused, failed, or completed jobs to continue scraping missing places without duplicates
            if (status === 'paused' || status === 'failed' || status === 'error' || status === 'ok') {
                actionsHtml += `<button class="resume-button" onclick="resumeJobSearch('${j.id}')" title="Resume search from saved results without duplicates">▶</button>`;
            }

            // Export & View buttons ("View Map" -> "Map", LeadsDB -> DB, CSV download linked on filename)
            if (status === 'ok' || status === 'paused' || status === 'working') {
                actionsHtml += `<button class="view-button" onclick="openMapModal('${j.id}', '${name}')" title="View interactive map">Map</button>`;
                actionsHtml += `<a href="/jobs/${j.id}/report" target="_blank" class="report-button" title="Interactive Bayesian Results Dashboard">Report</a>`;
                actionsHtml += `<a href="/api/v1/jobs/${j.id}/download?format=json" class="json-button" title="Download JSON (-json)">JSON</a>`;
                actionsHtml += `<button class="leads-button" onclick="exportToLeadsDB('${j.id}')" title="Export to LeadsDB (-leadsdb-api-key)">DB</button>`;
            }

            actionsHtml += `<button class="delete-button" onclick="deleteJob('${j.id}')">Delete</button>`;
            actionsHtml += '</div>';

            const shortId = id.substring(0, 8);
            const placesCount = j.places_count != null ? j.places_count : 0;
            const fileName = j.file_name || '—';
            const fileSize = j.file_size || '—';
            const startTime = j.start_time ? new Date(j.start_time).toLocaleString() : (j.date ? new Date(j.date).toLocaleString() : '—');
            const endTime = j.end_time ? new Date(j.end_time).toLocaleString() : (status === 'working' ? '<span style="color:#c29d62;font-weight:600;">In corso...</span>' : '—');

            // Format params for expandable row
            const jd = j.data || {};
            const kwList = Array.isArray(jd.keywords) ? jd.keywords.join(', ') : (jd.keywords || '—');
            const maxPlacesBadge = jd.max_places ? `<span style="background:rgba(244,63,94,0.18);color:#f87171;padding:2px 6px;border-radius:4px;font-weight:600;">Limite: ${jd.max_places}</span>` : 'Nessun limite';

            const fileCellHtml = fileName !== '—'
                ? `<a href="/api/v1/jobs/${id}/download?format=csv" title="Download ${fileName}" style="text-decoration:none;color:var(--color-primary-light);font-size:11.5px;font-family:ui-monospace,monospace;display:inline-block;white-space:nowrap;">📥 ${fileName}</a>
                   <div style="font-size:10.5px;color:var(--color-text-muted);margin-top:1px;font-family:ui-monospace,monospace;">💾 ${fileSize}</div>`
                : `<div style="font-size:11.5px;font-family:ui-monospace,monospace;color:var(--color-text-muted);">—</div>`;

            const isExpanded = openJobParamsSet.has(id);
            const arrowChar = isExpanded ? '▼' : '▶';
            const rowDisplayStyle = isExpanded ? '' : 'none';

            html += `<tr>
                <td title="Clicca per visualizzare/nascondere i parametri usati" style="white-space:nowrap;">
                    <span style="cursor:pointer;color:var(--color-primary-light);font-weight:600;display:inline-flex;align-items:center;gap:4px;" onclick="toggleJobParams('${id}')">
                        <span id="arrow-${id}">${arrowChar}</span> <code style="font-size:12px;">${shortId}…</code>
                    </span>
                </td>
                <td><strong style="color:var(--color-text);font-size:13.5px;">${name}</strong></td>
                <td style="text-align:center;"><span style="background:rgba(14,165,233,0.12);color:#38bdf8;padding:2px 7px;border-radius:4px;font-weight:700;font-size:12px;font-family:ui-monospace,monospace;">${placesCount}</span></td>
                <td>
                    ${fileCellHtml}
                </td>
                <td style="font-size:11.5px;line-height:1.35;white-space:nowrap;">
                    <div><span style="color:var(--color-text-muted);">Inizio:</span> ${startTime}</div>
                    <div><span style="color:var(--color-text-muted);">Fine:</span> ${endTime}</div>
                </td>
                <td style="text-align:center;">${statusHtml}</td>
                <td style="white-space:nowrap;">${actionsHtml}</td>
            </tr>
            <tr id="params-row-${id}" style="display:${rowDisplayStyle};background:rgba(20,24,29,0.7);">
                <td colspan="7" style="padding:14px 20px;border-bottom:2px solid var(--color-border);">
                    <div style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:6px;padding:12px 16px;font-size:13px;line-height:1.6;">
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;border-bottom:1px solid var(--color-border);padding-bottom:6px;">
                            <strong style="color:var(--color-primary-light);">⚙️ Parametri utilizzati per questa ricerca (Job ID: ${id})</strong>
                            <a href="/api/v1/jobs/${id}" target="_blank" style="color:var(--color-text-muted);font-size:11.5px;text-decoration:none;">Vedi JSON Raw ↗</a>
                        </div>
                        <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));gap:10px;">
                            <div><strong>Keywords:</strong> <span style="color:var(--color-text);">${escapeHtml(kwList)}</span></div>
                            <div><strong>Lingua:</strong> <span style="color:var(--color-text);">${escapeHtml(jd.lang || 'en')}</span></div>
                            <div><strong>Coordinate (Lat, Lon):</strong> <span style="color:var(--color-text);">${jd.lat || '0'}, ${jd.lon || '0'}</span></div>
                            <div><strong>Raggio:</strong> <span style="color:var(--color-text);">${jd.radius || '10000'} m</span></div>
                            <div><strong>Zoom:</strong> <span style="color:var(--color-text);">${jd.zoom || '15'}</span></div>
                            <div><strong>Depth (scroll):</strong> <span style="color:var(--color-text);">${jd.depth || '10'}</span></div>
                            <div><strong>Limite Locali:</strong> ${maxPlacesBadge}</div>
                            <div><strong>E-mail Scraping:</strong> <span style="color:var(--color-text);">${jd.email ? '✅ Sì' : '❌ No'}</span></div>
                            <div><strong>Extra Reviews:</strong> <span style="color:var(--color-text);">${jd.extra_reviews ? '✅ Sì' : '❌ No'}</span></div>
                            <div><strong>Fast Mode:</strong> <span style="color:var(--color-text);">${jd.fast_mode ? '⚡ Attivo' : 'Disattivato'}</span></div>
                        </div>
                    </div>
                </td>
            </tr>`;
        }
        tbody.innerHTML = html;
    }

    async function deleteJob(id) {
        if (!confirm('Delete this job and its results?')) return;
        try {
            await fetch('/api/v1/jobs/' + id, { method: 'DELETE' });
            fetchJobs();
        } catch (err) {
            alert('Error deleting job: ' + err.message);
        }
    }

    // ---- Map Modal (Zero Lag with MarkerCluster & Canvas) ----
    let leafletMap = null;
    let markersCluster = null;

    async function openMapModal(jobId, jobName) {
        const existing = document.getElementById('__map-modal');
        if (existing) existing.remove();

        const modal = document.createElement('div');
        modal.id = '__map-modal';
        modal.className = 'map-modal';
        modal.innerHTML = `
            <div class="map-modal-content">
                <div class="map-modal-header">
                    <h2>${jobName}</h2>
                    <button class="map-modal-close" onclick="closeMapModal()">&times;</button>
                </div>
                <div class="map-modal-body">
                    <div id="job-map"></div>
                    <div id="job-places-list"><div class="map-loading">Loading...</div></div>
                </div>
            </div>`;
        modal.addEventListener('click', (e) => { if (e.target === modal) closeMapModal(); });
        document.getElementById('map-modal-container').appendChild(modal);

        if (leafletMap) {
            leafletMap.remove();
            leafletMap = null;
            markersCluster = null;
        }

        leafletMap = L.map('job-map', {
            preferCanvas: true
        });

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '© OpenStreetMap contributors'
        }).addTo(leafletMap);

        markersCluster = L.markerClusterGroup({
            chunkedLoading: true,
            maxClusterRadius: 50,
            spiderfyOnMaxZoom: true,
            showCoverageOnHover: false,
            zoomToBoundsOnClick: true
        });
        leafletMap.addLayer(markersCluster);

        const listEl = document.getElementById('job-places-list');

        try {
            const res = await fetch('/api/v1/jobs/' + jobId + '/download?format=json');
            const data = await res.json();
            const places = data.places || [];

            listEl.innerHTML = '';
            if (places.length === 0) {
                listEl.innerHTML = '<div class="map-empty">No coordinates found for this job.</div>';
                leafletMap.setView([20, 0], 2);
                return;
            }

            // Render first 200 places in DOM sidebar to prevent UI freeze on 5.000+ items
            const placesToRender = places.slice(0, 200);
            placesToRender.forEach((place) => {
                const lat = parseFloat(place.latitude);
                const lon = parseFloat(place.longitude);
                const title = place.title || 'Place';
                const address = place.address || '';
                const phone = place.phone || '';
                const rating = place.review_rating || '';
                const reviewCount = place.review_count || '';
                const link = place.link || '';
                const website = (place.website || '').trim();
                const emails = (place.emails || place.email || '').trim();

                const item = document.createElement('div');
                item.className = 'place-item';
                item.innerHTML = `
                    <div class="place-title">${escapeHtml(title)}</div>
                    ${address ? `<div class="place-address">${escapeHtml(address)}</div>` : ''}
                    ${phone ? `<div class="place-address">📞 ${escapeHtml(phone)}</div>` : ''}
                    ${website ? `<div class="place-address">🌐 <a href="${encodeURI(website)}" target="_blank" style="color:#38bdf8;text-decoration:none;" onclick="event.stopPropagation();">${escapeHtml(website.replace('https://','').replace('http://','').replace(/\/$/,'').substring(0, 32))} ↗</a></div>` : ''}
                    ${emails ? `<div class="place-address">📧 <a href="mailto:${escapeHtml(emails.split(',')[0].trim())}" style="color:#34d399;text-decoration:none;" onclick="event.stopPropagation();">${escapeHtml(emails.split(',')[0].trim())}</a></div>` : ''}
                    ${rating ? `<div class="place-rating">★ ${escapeHtml(rating)} (${escapeHtml(reviewCount)} reviews)</div>` : ''}
                    ${link ? `<div><a href="${encodeURI(link)}" target="_blank" style="font-size:12px;color:#60a5fa;">Open on Google Maps ↗</a></div>` : ''}
                `;

                if (!isNaN(lat) && !isNaN(lon) && lat !== 0 && lon !== 0) {
                    item.addEventListener('click', () => {
                        document.querySelectorAll('.place-item').forEach(el => el.style.background = '');
                        item.style.background = 'rgba(59, 130, 246, 0.25)';
                        leafletMap.setView([lat, lon], 17);
                    });
                }

                listEl.appendChild(item);
            });

            if (places.length > 200) {
                const notice = document.createElement('div');
                notice.style.padding = '10px';
                notice.style.fontSize = '12px';
                notice.style.textAlign = 'center';
                notice.style.color = 'var(--color-text-light)';
                notice.innerHTML = `Mostrati primi 200 di ${places.length} luoghi nella lista laterale.<br>Tutti i punti sono caricati fluidamente sulla mappa. Scarica CSV/JSON per l'elenco integrale.`;
                listEl.appendChild(notice);
            }

            // Fast batch creation of circle markers via Canvas
            const markersList = [];
            for (let i = 0; i < places.length; i++) {
                const p = places[i];
                const lat = parseFloat(p.latitude);
                const lon = parseFloat(p.longitude);
                if (!isNaN(lat) && !isNaN(lon) && lat !== 0 && lon !== 0) {
                    const title = p.title || 'Place';
                    const addr = p.address || '';
                    const ph = p.phone || '';
                    const ws = (p.website || '').trim();
                    const em = (p.emails || p.email || '').trim();
                    const popupParts = [`<strong>${escapeHtml(title)}</strong>`];
                    if (addr) popupParts.push(escapeHtml(addr));
                    if (ph) popupParts.push('📞 ' + escapeHtml(ph));
                    if (ws) popupParts.push(`🌐 <a href="${encodeURI(ws)}" target="_blank" style="color:var(--color-primary-light);">${escapeHtml(ws.replace(/^https?:\/\//, '').substring(0,30))} ↗</a>`);
                    if (em) popupParts.push(`📧 <a href="mailto:${escapeHtml(em.split(',')[0].trim())}">${escapeHtml(em.split(',')[0].trim())}</a>`);
                    const m = L.circleMarker([lat, lon], {
                        radius: 6,
                        fillColor: "#3b82f6",
                        color: "#ffffff",
                        weight: 1,
                        opacity: 1,
                        fillOpacity: 0.8
                    }).bindPopup(popupParts.join('<br>'));
                    markersList.push(m);
                }
            }

            markersCluster.addLayers(markersList);

            if (markersList.length > 0) {
                leafletMap.fitBounds(markersCluster.getBounds(), { padding: [30, 30] });
            } else {
                leafletMap.setView([20, 0], 2);
            }

            setTimeout(() => { leafletMap.invalidateSize(); }, 200);

        } catch (err) {
            listEl.innerHTML = '<div class="map-error">Error loading data: ' + escapeHtml(err.message) + '</div>';
        }
    }

    function closeMapModal() {
        const modal = document.getElementById('__map-modal');
        if (modal) modal.remove();
        if (leafletMap) {
            leafletMap.remove();
            leafletMap = null;
            markersCluster = null;
        }
    }

    // ---- Expandable Job Parameters ----
    const openJobParamsSet = new Set();
    function toggleJobParams(id) {
        const row = document.getElementById('params-row-' + id);
        const arrow = document.getElementById('arrow-' + id);
        if (openJobParamsSet.has(id)) {
            openJobParamsSet.delete(id);
            if (row) row.style.display = 'none';
            if (arrow) arrow.textContent = '▶';
        } else {
            openJobParamsSet.add(id);
            if (row) row.style.display = '';
            if (arrow) arrow.textContent = '▼';
        }
    }

    // ---- Merge Jobs Modal (Multi-Job Checkbox & Quick Filter) ----
    function openMergeModal() {
        const existing = document.getElementById('__merge-modal');
        if (existing) existing.remove();

        const jobs = cachedJobs || [];
        let itemsHtml = '';
        jobs.forEach(j => {
            const count = j.places_count != null ? j.places_count : 0;
            const status = j.status || 'pending';
            const shortId = (j.id || '').substring(0, 8);
            const statusBadge = (status === 'ok' || status === 'paused')
                ? `<span style="background:rgba(16,185,129,0.15);color:#34d399;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:600;">${status}</span>`
                : `<span style="background:rgba(148,163,184,0.15);color:#94a3b8;padding:1px 6px;border-radius:4px;font-size:11px;">${status}</span>`;

            itemsHtml += `
                <label style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:#0f172a;border:1px solid #334155;border-radius:6px;cursor:pointer;user-select:none;transition:background 0.15s;" onmouseover="this.style.background='#1a2436'" onmouseout="this.style.background='#0f172a'">
                    <input type="checkbox" class="merge-job-cb" data-status="${status}" value="${j.id}" onchange="updateMergeCounter()" style="width:16px;height:16px;cursor:pointer;accent-color:var(--color-primary);">
                    <div style="flex:1;min-width:0;display:flex;justify-content:space-between;align-items:center;gap:8px;">
                        <span style="font-size:13px;font-weight:500;color:#f1f5f9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(j.name)}">${escapeHtml(j.name)}</span>
                        <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">
                            <span style="background:rgba(56,189,248,0.12);color:#38bdf8;padding:1px 6px;border-radius:4px;font-size:11.5px;font-family:monospace;">${count} loc.</span>
                            ${statusBadge}
                            <span style="color:#64748b;font-size:11px;font-family:monospace;">${shortId}</span>
                        </div>
                    </div>
                </label>`;
        });

        const modal = document.createElement('div');
        modal.id = '__merge-modal';
        modal.className = 'map-modal';
        modal.innerHTML = `
            <div class="map-modal-content" style="max-width:620px;height:auto;max-height:90vh;padding:24px;border-radius:10px;background:#1e293b;border:1px solid #334155;color:#f1f5f9;display:flex;flex-direction:column;">
                <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155;padding-bottom:12px;margin-bottom:14px;">
                    <div>
                        <h3 style="margin:0;font-size:1.18rem;color:#38bdf8;">🔀 Unisci Ricerche Multiple (Merge Deduplicato)</h3>
                        <p style="margin:4px 0 0 0;font-size:12px;color:#94a3b8;">Unisci 2 o più ricerche eliminando automaticamente i locali duplicati.</p>
                    </div>
                    <button class="map-modal-close" onclick="closeMergeModal()" style="font-size:24px;cursor:pointer;background:none;border:none;color:#94a3b8;">&times;</button>
                </div>
                
                <div style="display:flex;flex-direction:column;gap:14px;overflow-y:auto;padding-right:4px;">
                    <!-- Quick Select Action Buttons -->
                    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                        <span style="font-size:12.5px;font-weight:600;color:#94a3b8;">Seleziona le ricerche da unire:</span>
                        <div style="display:flex;gap:6px;">
                            <button type="button" onclick="selectMergeJobsByStatus('ok,paused')" style="padding:4px 10px;font-size:11.5px;background:#0369a1;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:600;">
                                ✅ Tutti con stato OK/PAUSED
                            </button>
                            <button type="button" onclick="toggleAllMergeJobs(true)" style="padding:4px 8px;font-size:11.5px;background:#334155;color:#f1f5f9;border:none;border-radius:4px;cursor:pointer;">
                                Tutti
                            </button>
                            <button type="button" onclick="toggleAllMergeJobs(false)" style="padding:4px 8px;font-size:11.5px;background:#334155;color:#f1f5f9;border:none;border-radius:4px;cursor:pointer;">
                                Nessuno
                            </button>
                        </div>
                    </div>

                    <!-- Scrollable list of jobs with checkboxes -->
                    <div id="merge-jobs-list" style="max-height:260px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:8px;background:#0b1120;border:1px solid #334155;border-radius:8px;">
                        ${itemsHtml || '<div style="color:#64748b;text-align:center;padding:16px;">Nessuna ricerca disponibile.</div>'}
                    </div>

                    <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#94a3b8;">
                        <span id="merge-selected-counter" style="color:#38bdf8;font-weight:600;">0 ricerche selezionate</span>
                    </div>

                    <div>
                        <label style="font-size:12.5px;font-weight:600;color:#94a3b8;display:block;margin-bottom:6px;">Nome della nuova ricerca unita (opzionale):</label>
                        <input type="text" id="merge-job-name" placeholder="es. Ristoranti Totali (Uniti) o lascia vuoto per nome automatico" style="width:100%;box-sizing:border-box;padding:10px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#f1f5f9;font-size:13px;">
                    </div>

                    <div style="font-size:12px;color:#94a3b8;background:rgba(2,132,199,0.1);border:1px solid rgba(2,132,199,0.3);border-radius:6px;padding:10px;">
                        ℹ️ L'unione creerà un nuovo Job e un nuovo file CSV combinando tutti i record dei job scelti, eliminando i duplicati basandosi su link, place_id e nome+indirizzo.
                    </div>

                    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:6px;">
                        <button type="button" onclick="closeMergeModal()" style="padding:8px 16px;background:#334155;color:#f1f5f9;border:none;border-radius:6px;cursor:pointer;font-weight:600;">Annulla</button>
                        <button type="button" id="btn-confirm-merge" onclick="submitMergeJobs()" style="padding:8px 20px;background:var(--color-primary);color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;display:inline-flex;align-items:center;gap:6px;">
                            <span>🔀 Unisci Selezionati</span>
                        </button>
                    </div>
                </div>
            </div>`;
        modal.addEventListener('click', (e) => { if (e.target === modal) closeMergeModal(); });
        document.body.appendChild(modal);

        updateMergeCounter();
    }

    function toggleAllMergeJobs(select) {
        document.querySelectorAll('.merge-job-cb').forEach(cb => {
            cb.checked = select;
        });
        updateMergeCounter();
    }

    function selectMergeJobsByStatus(statusListStr) {
        const statuses = statusListStr.split(',').map(s => s.trim().toLowerCase());
        document.querySelectorAll('.merge-job-cb').forEach(cb => {
            const st = (cb.dataset.status || '').toLowerCase();
            cb.checked = statuses.includes(st);
        });
        updateMergeCounter();
    }

    function updateMergeCounter() {
        const cbs = Array.from(document.querySelectorAll('.merge-job-cb'));
        const selected = cbs.filter(cb => cb.checked);
        const counterEl = document.getElementById('merge-selected-counter');
        const btn = document.getElementById('btn-confirm-merge');
        if (counterEl) {
            counterEl.textContent = `${selected.length} di ${cbs.length} ricerche selezionate`;
        }
        if (btn) {
            btn.disabled = selected.length < 2;
            btn.style.opacity = selected.length < 2 ? '0.6' : '1';
        }
    }

    function closeMergeModal() {
        const modal = document.getElementById('__merge-modal');
        if (modal) modal.remove();
    }

    async function submitMergeJobs() {
        const selectedCbs = Array.from(document.querySelectorAll('.merge-job-cb')).filter(cb => cb.checked);
        const jobIds = selectedCbs.map(cb => cb.value);
        const name = (document.getElementById('merge-job-name')?.value || '').trim();

        if (jobIds.length < 2) {
            alert('Seleziona almeno 2 ricerche distinte da unire.');
            return;
        }

        const btn = document.getElementById('btn-confirm-merge');
        btn.disabled = true;
        btn.textContent = 'Unione in corso...';

        try {
            const res = await fetch('/api/v1/jobs/merge', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_ids: jobIds, name: name })
            });
            const data = await res.json();
            if (!res.ok) {
                alert("Errore: " + (data.error || "Impossibile completare l'unione"));
            } else {
                alert(data.message || 'Ricerche unite con successo!');
                closeMergeModal();
                await fetchJobs();
            }
        } catch (err) {
            alert('Errore di connessione: ' + err.message);
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔀 Unisci Selezionati';
            }
        }
    }

    // ---- Init ----
    fetchJobs();
    setInterval(fetchJobs, 3000);
    </script>
</body>
</html>
"""

def generate_grid_points(bbox_str, cell_km=1.0):
    try:
        parts = [float(p.strip()) for p in bbox_str.split(",")]
        if len(parts) != 4:
            return []
        min_lat, min_lon, max_lat, max_lon = parts
        points = []
        lat_step = cell_km / 111.0
        lat = min_lat
        while lat <= max_lat:
            cos_val = math.cos(math.radians(lat))
            lon_step = cell_km / (111.0 * (cos_val if abs(cos_val) > 0.01 else 0.01))
            lon = min_lon
            while lon <= max_lon:
                points.append((round(lat, 6), round(lon, 6)))
                lon += lon_step
            lat += lat_step
        return points
    except Exception:
        return []

def format_job_clean(raw_job):
    if not isinstance(raw_job, dict):
        return raw_job
    d = raw_job.get("Data") or raw_job.get("data") or {}
    max_time_raw = d.get("max_time", 0)
    max_time_sec = int(max_time_raw / 1_000_000_000) if max_time_raw > 1_000_000 else max_time_raw
    return {
        "id": raw_job.get("ID") or raw_job.get("id"),
        "name": raw_job.get("Name") or raw_job.get("name"),
        "date": raw_job.get("Date") or raw_job.get("date"),
        "status": raw_job.get("Status") or raw_job.get("status"),
        "data": {
            "keywords": d.get("keywords", []),
            "lang": d.get("lang", "en"),
            "zoom": d.get("zoom", 15),
            "lat": d.get("lat", ""),
            "lon": d.get("lon", ""),
            "radius": d.get("radius", 10000),
            "depth": d.get("depth", 10),
            "max_time_seconds": max_time_sec,
            "email": d.get("email", False),
            "fast_mode": d.get("fast_mode", False),
            "extra_reviews": d.get("extra_reviews", False),
            "proxies": d.get("proxies", [])
        }
    }

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

# ---- Pause / Resume All Jobs Engine Control ----
@app.route("/api/engine/status", methods=["GET"])
def engine_status():
    res = docker_action("gmaps-backend", "json")
    if res.get("status") == "ok":
        paused = bool(res.get("state", {}).get("Paused", False))
        return jsonify({"status": "ok", "paused": paused, "state": res.get("state", {}).get("Status")})
    return jsonify({"status": "unknown", "paused": False}), 200

@app.route("/api/engine/pause", methods=["POST"])
def engine_pause():
    res = docker_action("gmaps-backend", "pause")
    return jsonify({"action": "pause", "paused": True, "result": res})

@app.route("/api/engine/resume", methods=["POST"])
def engine_resume():
    res = docker_action("gmaps-backend", "unpause")
    return jsonify({"action": "resume", "paused": False, "result": res})

# ---- Per-Job Pause & Deduplicated Resume Endpoints ----
@app.route("/api/v1/jobs/<job_id>/pause", methods=["POST"])
def pause_individual_job(job_id):
    """
    Pauses an active scraping job:
    1. Sets job status to 'paused' in SQLite database.
    2. Cancels job in gmaps-backend if running there.
    3. Stops any running CLI runner container for this job.
    4. Results accumulated so far remain safely in <job_id>.csv on disk.
    """
    try:
        # 1. Stop background CLI runner container if one is running
        stop_job_runner(job_id)

        # 2. Cancel/Stop job in gmaps-backend if active there
        try:
            del_req = urllib.request.Request(f"{BACKEND_URL}/api/v1/jobs/{job_id}", method="DELETE")
            urllib.request.urlopen(del_req, timeout=3)
        except Exception:
            pass

        try:
            del_req2 = urllib.request.Request(f"{BACKEND_URL}/delete?id={job_id}", method="DELETE")
            urllib.request.urlopen(del_req2, timeout=3)
        except Exception:
            pass

        # 3. Update status to 'paused' in SQLite
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Database not accessible"}), 500

        cur = conn.cursor()
        cur.execute("UPDATE jobs SET status = 'paused', updated_at = ? WHERE id = ?", (int(time.time()), job_id))
        conn.commit()
        conn.close()

        return jsonify({"id": job_id, "status": "paused", "message": "Search paused. Results so far are saved in place."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/v1/jobs/<job_id>/resume", methods=["POST"])
def resume_individual_job(job_id):
    """
    Resumes a paused, interrupted, or finished job without spawning any new job:
    1. Loads the original job configuration.
    2. Creates and runs a dedicated CLI runner container on <job_id>.csv with -resume.
    3. Keeps the same single job ID, updates status to 'working', and appends new places in place.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Database not accessible"}), 500

        cur = conn.cursor()
        cur.execute("SELECT id, name, status, data FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Job not found"}), 404

        raw_data = json.loads(row["data"]) if row["data"] else {}
        name = row["name"] or "Job"

        # Prepare input keywords file for this specific job
        keywords = raw_data.get("keywords") or [name]
        input_file_name = f"input_{job_id[:8]}.txt"
        local_input_path = os.path.join(DATA_DIR, input_file_name)
        with open(local_input_path, "w", encoding="utf-8") as f:
            for kw in keywords:
                f.write(f"{kw.strip()}\n")

        # 1. Update status in DB to working
        cur.execute("UPDATE jobs SET status = 'working', updated_at = ? WHERE id = ?", (int(time.time()), job_id))
        conn.commit()
        conn.close()

        # 2. Build CLI runner arguments operating strictly on the single output file <job_id>.csv
        lang = str(raw_data.get("lang") or "en")
        zoom = int(raw_data.get("zoom") or 15)
        depth = int(raw_data.get("depth") or 10)
        radius = float(raw_data.get("radius") or 10000)
        lat = str(raw_data.get("lat") or "")
        lon = str(raw_data.get("lon") or "")
        exit_on_inactivity = str(raw_data.get("exit_on_inactivity") or "5m")

        cmd = [
            "-input", f"/data/{input_file_name}",
            "-results", f"/data/{job_id}.csv",
            "-resume",
            "-lang", lang,
            "-zoom", str(zoom),
            "-depth", str(depth),
            "-radius", str(radius),
            "-exit-on-inactivity", exit_on_inactivity,
            "-c", "4",
            "-pages-per-browser", "4",
            "-browser-pool-size", "2"
        ]

        if lat and lon and lat != "0" and lon != "0":
            cmd.extend(["-geo", f"{lat},{lon}"])
        if bool(raw_data.get("email", False)):
            cmd.append("-email")
        if bool(raw_data.get("fast_mode", False)):
            cmd.append("-fast-mode")
        if bool(raw_data.get("extra_reviews", False)):
            cmd.append("-extra-reviews")
        if bool(raw_data.get("disable_page_reuse", False)):
            cmd.append("-disable-page-reuse")

        proxies = raw_data.get("proxies") or []
        if proxies:
            cmd.extend(["-proxies", ",".join(proxies)])

        # Stop any previous runner for this job if still active
        stop_job_runner(job_id)

        # 3. Create and launch Docker runner container using the host-mounted data folder
        host_data = get_host_data_dir()
        container_name = f"gmaps-runner-{job_id}"
        create_payload = {
            "Image": "gosom/google-maps-scraper:latest",
            "Cmd": cmd,
            "HostConfig": {
                "Binds": [f"{host_data}:/data:rw"]
            }
        }

        status_code, resp_text = docker_api_request("POST", f"/v1.44/containers/create?name={container_name}", create_payload)
        if status_code != 201:
            return jsonify({"error": f"Failed to create runner container: {resp_text}"}), 500

        c_info = json.loads(resp_text)
        cid = c_info.get("Id")

        start_status, start_resp = docker_api_request("POST", f"/v1.44/containers/{cid}/start")
        if start_status not in (204, 200):
            return jsonify({"error": f"Failed to start runner: {start_resp}"}), 500

        # Spawn monitor thread to handle completion and update status
        threading.Thread(target=monitor_job_runner, args=(job_id, cid), daemon=True).start()

        return jsonify({
            "status": "working",
            "job_id": job_id,
            "message": "Job resumed in place. Appending results directly without creating duplicate jobs."
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---- Standard REST Endpoints (/api/v1/jobs) ----
@app.route("/api/v1/jobs", methods=["GET"])
def list_jobs():
    try:
        conn = get_db_connection()
        if not conn:
            req = urllib.request.Request(f"{BACKEND_URL}/api/v1/jobs")
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                cleaned = [format_job_clean(j) for j in data]
                return Response(json.dumps(cleaned, indent=2, ensure_ascii=False), mimetype="application/json")

        cur = conn.cursor()
        cur.execute("SELECT id, name, status, data, created_at, updated_at FROM jobs ORDER BY created_at DESC")
        rows = cur.fetchall()
        jobs = []
        for r in rows:
            d = json.loads(r["data"]) if r["data"] else {}
            start_date_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["created_at"])) if r["created_at"] else ""
            
            # Finished date if job is completed or stopped
            updated_ts = r["updated_at"]
            is_active = r["status"] in ("working", "pending")
            end_date_str = ""
            if not is_active and updated_ts and updated_ts > (r["created_at"] or 0):
                end_date_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated_ts))

            max_time_raw = d.get("max_time", 0)
            max_time_sec = int(max_time_raw / 1_000_000_000) if max_time_raw > 1_000_000 else max_time_raw

            file_name, file_size_str, places_count = get_file_stats(r["id"])

            jobs.append({
                "id": r["id"],
                "name": r["name"],
                "date": start_date_str,
                "start_time": start_date_str,
                "end_time": end_date_str,
                "status": r["status"],
                "file_name": file_name,
                "file_size": file_size_str,
                "places_count": places_count,
                "data": {
                    "keywords": d.get("keywords", []),
                    "lang": d.get("lang", "en"),
                    "zoom": d.get("zoom", 15),
                    "lat": d.get("lat", ""),
                    "lon": d.get("lon", ""),
                    "radius": d.get("radius", 10000),
                    "depth": d.get("depth", 10),
                    "max_places": d.get("max_places"),
                    "max_time_seconds": max_time_sec,
                    "email": d.get("email", False),
                    "fast_mode": d.get("fast_mode", False),
                    "extra_reviews": d.get("extra_reviews", False),
                    "proxies": d.get("proxies", [])
                }
            })
        conn.close()
        return Response(json.dumps(jobs, indent=2, ensure_ascii=False), mimetype="application/json")
    except Exception as e:
        return jsonify([]), 200

@app.route("/api/v1/jobs", methods=["POST"])
def create_job():
    try:
        payload = request.json or {}

        grid_bbox = payload.get("grid_bbox")
        grid_cell = float(payload.get("grid_cell") or 1.0)

        def build_req_payload(name, lat, lon):
            return {
                "name": name,
                "keywords": payload.get("keywords", []),
                "lang": payload.get("lang", "en"),
                "zoom": int(payload.get("zoom", 15)),
                "depth": int(payload.get("depth", 10)),
                "radius": int(payload.get("radius", 10000)),
                "max_time": int(payload.get("max_time", 600)),
                "email": bool(payload.get("email", False)),
                "fast_mode": bool(payload.get("fast_mode", False)),
                "extra_reviews": bool(payload.get("extra_reviews", False)),
                "disable_page_reuse": bool(payload.get("disable_page_reuse", False)),
                "exit_on_inactivity": str(payload.get("exit_on_inactivity") or "5m"),
                "debug": bool(payload.get("debug", False)),
                "writer": payload.get("writer"),
                "dsn": payload.get("dsn"),
                "produce": bool(payload.get("produce", False)),
                "s3_bucket": payload.get("s3_bucket"),
                "aws_region": payload.get("aws_region"),
                "function_name": payload.get("function_name"),
                "aws_access_key": payload.get("aws_access_key"),
                "aws_secret_key": payload.get("aws_secret_key"),
                "aws_lambda": bool(payload.get("aws_lambda", False)),
                "aws_lambda_invoker": bool(payload.get("aws_lambda_invoker", False)),
                "aws_lambda_chunk_size": int(payload.get("aws_lambda_chunk_size") or 100),
                "lat": str(lat or ""),
                "lon": str(lon or ""),
                "proxies": payload.get("proxies", [])
            }

        if grid_bbox:
            grid_points = generate_grid_points(grid_bbox, grid_cell)
            if not grid_points:
                return Response("Invalid grid-bbox format. Expected minLat,minLon,maxLat,maxLon", status=400)

            created_ids = []
            base_name = payload.get("name", "Grid Job")
            for idx, (g_lat, g_lon) in enumerate(grid_points[:30], start=1):
                cell_payload = build_req_payload(f"{base_name} [Cell {idx}/{len(grid_points)}]", g_lat, g_lon)
                req_data = json.dumps(cell_payload).encode("utf-8")
                req = urllib.request.Request(
                    f"{BACKEND_URL}/api/v1/jobs",
                    data=req_data,
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode())
                    created_ids.append(data.get("id"))
            return jsonify({"status": "created", "grid_cells": len(created_ids), "ids": created_ids}), 201

        cleaned_payload = build_req_payload(payload.get("name"), payload.get("lat"), payload.get("lon"))
        max_places_val = payload.get("max_places")
        if max_places_val:
            try:
                cleaned_payload["max_places"] = int(max_places_val)
            except Exception:
                pass
        req_data = json.dumps(cleaned_payload).encode("utf-8")
        req = urllib.request.Request(
            f"{BACKEND_URL}/api/v1/jobs",
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            new_id = data.get("id") or data.get("ID")
            if new_id and payload.get("max_places"):
                try:
                    conn = get_db_connection()
                    if conn:
                        cur = conn.cursor()
                        cur.execute("SELECT data FROM jobs WHERE id = ?", (new_id,))
                        r_data = cur.fetchone()
                        if r_data:
                            existing_d = json.loads(r_data["data"]) if r_data["data"] else {}
                            existing_d["max_places"] = int(payload.get("max_places"))
                            cur.execute("UPDATE jobs SET data = ? WHERE id = ?", (json.dumps(existing_d), new_id))
                            conn.commit()
                        conn.close()
                except Exception:
                    pass
            return jsonify(data), 201

    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        return Response(err_msg, status=e.code)
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/api/v1/jobs/merge", methods=["POST"])
def merge_jobs_endpoint():
    """
    Merges multiple existing jobs into a new job and local CSV file without duplicates.
    Supports either job_ids: [id1, id2, ...] or backwards-compatible job_id_1 & job_id_2.
    """
    try:
        body = request.json or {}
        job_ids = body.get("job_ids") or []
        if not job_ids:
            # Fallback backwards-compatibility
            j1 = (body.get("job_id_1") or "").strip()
            j2 = (body.get("job_id_2") or "").strip()
            if j1 and j2:
                job_ids = [j1, j2]

        # Clean and deduplicate list of IDs while preserving order
        clean_job_ids = []
        for jid in job_ids:
            jid = str(jid).strip()
            if jid and jid not in clean_job_ids:
                clean_job_ids.append(jid)

        custom_name = (body.get("name") or "").strip()

        if len(clean_job_ids) < 2:
            return jsonify({"error": "Seleziona almeno 2 ricerche differenti da unire."}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Database non accessibile."}), 500

        cur = conn.cursor()
        placeholders = ",".join("?" for _ in clean_job_ids)
        cur.execute(f"SELECT id, name, data, status FROM jobs WHERE id IN ({placeholders})", clean_job_ids)
        rows = cur.fetchall()
        jobs_map = {r["id"]: r for r in rows}

        valid_jobs = [jobs_map[jid] for jid in clean_job_ids if jid in jobs_map]
        if len(valid_jobs) < 2:
            conn.close()
            return jsonify({"error": "Meno di 2 job validi trovati nel database tra quelli selezionati."}), 404

        new_job_id = str(uuid.uuid4())
        if custom_name:
            new_name = custom_name
        else:
            names_summary = " + ".join([j["name"] for j in valid_jobs[:3]])
            if len(valid_jobs) > 3:
                names_summary += f" + altri {len(valid_jobs) - 3}"
            new_name = f"Unione ({len(valid_jobs)} ricerche): {names_summary}"

        # Combine keywords and metadata from all jobs
        combined_keywords = []
        combined_data = {}
        for idx, j in enumerate(valid_jobs):
            dj = json.loads(j["data"]) if j["data"] else {}
            if idx == 0:
                combined_data = dict(dj)
            kw = dj.get("keywords") or []
            for k in kw:
                if k not in combined_keywords:
                    combined_keywords.append(k)

        combined_data["keywords"] = combined_keywords
        combined_data["merged_from"] = [j["id"] for j in valid_jobs]

        new_target_csv = os.path.join(DATA_DIR, f"{new_job_id}.csv")

        # Merge CSV from each job
        for j in valid_jobs:
            jid = j["id"]
            csv_path = os.path.join(DATA_DIR, f"{jid}.csv")
            if os.path.exists(csv_path):
                with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
                    csv_content = f.read()
                if csv_content:
                    deduplicate_and_merge_csv(new_target_csv, csv_content)

        # Insert new record into jobs.db
        now_ts = int(time.time())
        cur.execute(
            "INSERT INTO jobs (id, name, status, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_job_id, new_name, "ok", json.dumps(combined_data), now_ts, now_ts)
        )
        conn.commit()
        conn.close()

        file_name, file_size_str, places_count = get_file_stats(new_job_id)

        return jsonify({
            "status": "ok",
            "message": f"Unione completata con successo! {places_count} locali unici estratti da {len(valid_jobs)} ricerche.",
            "job": {
                "id": new_job_id,
                "name": new_name,
                "file_name": file_name,
                "file_size": file_size_str,
                "places_count": places_count
            }
        }), 201

    except Exception as e:
        return jsonify({"error": f"Errore durante l'unione: {str(e)}"}), 500

@app.route("/api/docs")
def api_docs():
    try:
        req = urllib.request.Request(f"{BACKEND_URL}/api/docs")
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            return Response(content, mimetype="text/html")
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/static/spec/<path:subpath>")
def proxy_static_spec(subpath):
    try:
        req = urllib.request.Request(f"{BACKEND_URL}/static/spec/{subpath}")
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
            mimetype = "application/x-yaml" if subpath.endswith((".yaml", ".yml")) else "application/json"
            return Response(content, mimetype=mimetype)
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/api/v1/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    try:
        req = urllib.request.Request(f"{BACKEND_URL}/api/v1/jobs/{job_id}")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            formatted = format_job_clean(data)
            return Response(json.dumps(formatted, indent=2, ensure_ascii=False), mimetype="application/json")
    except urllib.error.HTTPError as e:
        return Response(e.read().decode("utf-8"), status=e.code)
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/api/v1/jobs/<job_id>", methods=["DELETE"])
def delete_job_v1(job_id):
    try:
        stop_job_runner(job_id)
        # Delete from backend (try both /api/v1/jobs/{id} and /delete?id={id})
        try:
            req = urllib.request.Request(f"{BACKEND_URL}/api/v1/jobs/{job_id}", method="DELETE")
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
        try:
            req2 = urllib.request.Request(f"{BACKEND_URL}/delete?id={job_id}", method="DELETE")
            urllib.request.urlopen(req2, timeout=3)
        except Exception:
            pass

        # Also delete directly from SQLite if present
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                conn.commit()
                conn.close()
        except Exception:
            pass

        # Delete any associated files on disk
        for fname in (f"{job_id}.csv", f"{job_id}.csv.resume.json", f"input_{job_id[:8]}.txt"):
            fpath = os.path.join(DATA_DIR, fname)
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass

        return jsonify({"status": "deleted", "id": job_id}), 200
    except urllib.error.HTTPError as e:
        return Response(e.read().decode("utf-8"), status=e.code)
    except Exception as e:
        return Response(str(e), status=500)

@app.route("/api/v1/jobs/<job_id>/download", methods=["GET"])
def download_job_v1(job_id):
    fmt = request.args.get("format", "csv").lower()
    try:
        # Check local data folder first for resumed/merged CSV
        local_csv = os.path.join(DATA_DIR, f"{job_id}.csv")
        if os.path.exists(local_csv):
            with open(local_csv, "rb") as f:
                content = f.read()
        else:
            req = urllib.request.Request(f"{BACKEND_URL}/download?id={job_id}")
            with urllib.request.urlopen(req) as resp:
                content = resp.read()

        if fmt == "json":
            text = content.decode("utf-8", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            items = []
            for row in reader:
                converted = {}
                for k, v in row.items():
                    if isinstance(v, str) and (v.startswith('{') or v.startswith('[')):
                        try:
                            converted[k] = json.loads(v)
                        except Exception:
                            converted[k] = v
                    else:
                        converted[k] = v
                items.append(converted)
            json_response = {
                "job_id": job_id,
                "total_results": len(items),
                "places": items
            }
            return Response(
                json.dumps(json_response, indent=2, ensure_ascii=False),
                mimetype="application/json",
                headers={"Content-Disposition": f"attachment; filename=gmaps_{job_id[:8]}.json"}
            )
        else:
            return Response(
                content,
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename=gmaps_{job_id[:8]}.csv"}
            )
    except Exception as e:
        return Response(f"Download error: {e}", status=500)

# Backwards compatibility legacy aliases
@app.route("/api/jobs", methods=["GET"])
def legacy_get_jobs():
    return list_jobs()

@app.route("/api/jobs", methods=["POST"])
def legacy_post_jobs():
    return create_job()

@app.route("/download/<job_id>")
def legacy_download(job_id):
    return download_job_v1(job_id)

@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def legacy_delete(job_id):
    return delete_job_v1(job_id)

@app.route("/api/export/leadsdb/<job_id>", methods=["POST"])
def export_leadsdb(job_id):
    try:
        data = request.json or {}
        api_key = data.get("api_key")
        if not api_key:
            return jsonify({"error": "Missing LeadsDB API key"}), 400

        local_csv = os.path.join(DATA_DIR, f"{job_id}.csv")
        if os.path.exists(local_csv):
            with open(local_csv, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            req = urllib.request.Request(f"{BACKEND_URL}/download?id={job_id}")
            with urllib.request.urlopen(req) as resp:
                text = resp.read().decode("utf-8", errors="replace")

        reader = csv.DictReader(io.StringIO(text))
        csv_rows = list(reader)

        if not csv_rows:
            return jsonify({"error": "No records found to export"}), 400

        # Transform CSV records to LeadsDB payload specification
        leads = []
        for r in csv_rows:
            name = (r.get("title") or r.get("name") or "").strip()
            if not name:
                continue

            address = (r.get("address") or "").strip()
            website = (r.get("website") or "").strip()
            phone = (r.get("phone") or "").strip()
            email = (r.get("email") or r.get("emails") or "").strip()

            # Clean emails if comma-separated
            if email and "," in email:
                email = email.split(",")[0].strip()

            lead = {
                "name": name,
                "source": "google_maps",
                "address": address,
                "website": website,
                "phone": phone,
                "email": email,
                "attributes": []
            }

            # Map additional useful metadata attributes (with type='text')
            attr_mappings = [
                ("category", r.get("category")),
                ("place_id", r.get("place_id")),
                ("rating", r.get("review_rating")),
                ("review_count", r.get("review_count")),
                ("latitude", r.get("latitude")),
                ("longitude", r.get("longitude")),
                ("link", r.get("link")),
                ("cid", r.get("cid"))
            ]
            for attr_name, attr_val in attr_mappings:
                if attr_val:
                    lead["attributes"].append({
                        "name": attr_name,
                        "type": "text",
                        "value": str(attr_val).strip()
                    })

            leads.append(lead)

        if not leads:
            return jsonify({"error": "No valid leads extracted from dataset"}), 400

        # LeadsDB batch API endpoint
        leadsdb_url = "https://getleadsdb.com/api/v1/leads/batch"
        leads_req = urllib.request.Request(
            leadsdb_url,
            data=json.dumps({"leads": leads}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )
        try:
            with urllib.request.urlopen(leads_req) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                success_count = resp_data.get("success", len(leads))
                return jsonify({
                    "status": "exported",
                    "count": success_count,
                    "total": len(leads),
                    "response": resp_data
                })
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")
            return jsonify({"error": f"LeadsDB API error ({he.code}): {err_body}", "count": len(leads)}), he.code

    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ==========================================
# --- REPORT DASHBOARD GENERATOR LOGIC ---
# ==========================================
def calculate_distance(lat1, lon1, lat2, lon2):
    try:
        R = 6371.0
        lat1, lon1, lat2, lon2 = map(math.radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return round(R * c, 2)
    except Exception:
        return 9999.99

def format_open_hours(hours_str):
    if not hours_str or hours_str == "{}" or hours_str == "None":
        return "<span style=\"color:#64748b;font-style:italic;\">Orari non disponibili</span>"
    try:
        hours = json.loads(hours_str) if isinstance(hours_str, str) else hours_str
        days = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
        pills = []
        for day in days:
            if day in hours:
                times = ", ".join(hours[day])
                is_closed = "chiuso" in times.lower()
                bg = "rgba(239, 68, 68, 0.15)" if is_closed else "rgba(16, 185, 129, 0.1)"
                color = "#f87171" if is_closed else "#34d399"
                pills.append(f'<div style="display:flex;justify-content:space-between;padding:4px 8px;border-radius:4px;background:{bg};margin-bottom:3px;font-size:12px;"><span style="color:#94a3b8;font-weight:600;">{day[:3].capitalize()}</span><span style="color:{color};font-weight:500;">{times}</span></div>')
        return "".join(pills) if pills else "<span style=\"color:#64748b;font-style:italic;\">Orari non disponibili</span>"
    except Exception:
        return str(hours_str)

def get_province_from_address(addr_str):
    if not addr_str or addr_str == "None":
        return "Sconosciuta"
    try:
        addr = json.loads(addr_str) if isinstance(addr_str, str) else addr_str
        return (addr.get("state") or addr.get("city") or "Sconosciuta").strip()
    except Exception:
        return "Sconosciuta"

def format_reviews(reviews_str):
    if not reviews_str or reviews_str == "None":
        return "<p style=\"color:#64748b;font-size:12.5px;\">Nessuna recensione registrata per questo locale.</p>"
    try:
        reviews = json.loads(reviews_str) if isinstance(reviews_str, str) else reviews_str
        if not reviews:
            return "<p style=\"color:#64748b;font-size:12.5px;\">Nessuna recensione registrata per questo locale.</p>"
        html_out = ['<div class="reviews-container">']
        for rev in reviews:
            name = rev.get("Name", "Utente Anonimo")
            try:
                rating = int(rev.get("Rating", 0))
            except Exception:
                rating = 0
            stars = "★" * rating + "☆" * max(0, 5 - rating)
            when = rev.get("When", "")
            desc = (rev.get("Description") or "").replace("\n", "<br>")
            pic = rev.get("ProfilePicture", "")
            reply = rev.get("reply_text_original") or ""
            reply_html = f'<div style="margin-top:8px;padding:8px 12px;border-left:2px solid #38bdf8;background:rgba(56,189,248,0.06);border-radius:0 6px 6px 0;font-size:11.5px;color:#94a3b8;"><strong style="color:#38bdf8;">Risposta proprietario:</strong><br>{reply}</div>' if reply else ""
            img_tag = f'<img src="{pic}" class="review-avatar" alt="{name}">' if pic else '<div class="review-avatar-placeholder"></div>'
            html_out.append(f"""
            <div class="review-card">
                <div class="review-header">
                    {img_tag}
                    <div style="flex:1;">
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <strong style="color:#f1f5f9;font-size:12.5px;">{name}</strong>
                            <span style="font-size:11px;color:#64748b;">{when}</span>
                        </div>
                        <div class="review-stars">{stars} <span style="font-size:11px;color:#94a3b8;font-weight:600;">({rating}/5)</span></div>
                    </div>
                </div>
                <div class="review-body">{desc}</div>
                {reply_html}
            </div>
            """)
        html_out.append('</div>')
        return "".join(html_out)
    except Exception:
        return "<p style=\"color:#f87171;font-size:12.5px;\">Formato recensioni non valido.</p>"

def render_job_report_html(job_id, job_name, csv_text, user_lat=None, user_lng=None, job_geo_lat=None, job_geo_lng=None):
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Nessun Risultato</title></head>
<body style="font-family:sans-serif;padding:40px;text-align:center;">
    <h2>Nessun dato o luogo trovato per questo job.</h2>
    <p>Assicurati che lo scraping sia stato avviato e abbia generato risultati.</p>
</body></html>"""

    # Determine reference coordinates (center/centroid or default: Arena di Verona)
    # Priority:
    # 1. User URL params (?lat=..&lng=..)
    # 2. Search job parameters (-geo: lat, lon)
    # 3. Default: Coordinate Arena di Verona (45.4389958, 10.9943574)
    ARENA_VERONA_LAT = 45.4389958
    ARENA_VERONA_LNG = 10.9943574

    if user_lat is not None and user_lng is not None:
        try:
            start_lat = float(user_lat)
            start_lng = float(user_lng)
            has_custom_coords = True
        except Exception:
            start_lat = ARENA_VERONA_LAT
            start_lng = ARENA_VERONA_LNG
            has_custom_coords = False
    elif job_geo_lat is not None and job_geo_lng is not None and (float(job_geo_lat) != 0.0 or float(job_geo_lng) != 0.0):
        try:
            start_lat = float(job_geo_lat)
            start_lng = float(job_geo_lng)
            has_custom_coords = True
        except Exception:
            start_lat = ARENA_VERONA_LAT
            start_lng = ARENA_VERONA_LNG
            has_custom_coords = False
    else:
        start_lat = ARENA_VERONA_LAT
        start_lng = ARENA_VERONA_LNG
        has_custom_coords = False

    ratings_for_valid = []
    counts_for_valid = []
    processed_rows = []

    for r in rows:
        try:
            rc = float(r.get("review_count") or 0)
        except Exception:
            rc = 0.0
        try:
            rr = float(r.get("review_rating") or 0)
        except Exception:
            rr = 0.0

        if rc > 0:
            ratings_for_valid.append(rr)
            counts_for_valid.append(rc)

        lat_val = r.get("latitude") or 0
        lng_val = r.get("longitude") or 0
        dist = calculate_distance(start_lat, start_lng, lat_val, lng_val)
        prov = get_province_from_address(r.get("complete_address") or r.get("address") or "")

        row_copy = dict(r)
        row_copy["_review_count"] = rc
        row_copy["_review_rating"] = rr
        row_copy["_dist_km"] = dist
        row_copy["_provincia"] = prov
        processed_rows.append(row_copy)

    # Bayesian rating score parameters
    if ratings_for_valid:
        global_m = sum(ratings_for_valid) / len(ratings_for_valid)
        sorted_counts = sorted(counts_for_valid)
        n = len(sorted_counts)
        if n % 2 == 1:
            global_c = sorted_counts[n // 2]
        else:
            global_c = (sorted_counts[n // 2 - 1] + sorted_counts[n // 2]) / 2.0
        if global_c < 10:
            global_c = 10.0
    else:
        global_m = 0.0
        global_c = 50.0

    for r in processed_rows:
        v = r["_review_count"]
        R = r["_review_rating"]
        if v == 0:
            r["_bayesian"] = 0.0
        else:
            r["_bayesian"] = ((global_c * global_m) + (v * R)) / (global_c + v)

    unique_provinces = sorted(list(set(r["_provincia"] for r in processed_rows if r["_provincia"] and r["_provincia"] != "Sconosciuta")))
    prov_options = "".join(f'<option value="{p}">{p}</option>' for p in unique_provinces)

    EXCLUDED_COLS = {
        "title", "address", "phone", "emails", "review_rating", "review_count",
        "category", "website", "open_hours", "latitude", "longitude", "place_id",
        "user_reviews", "user_reviews_extended", "link",
        "_review_count", "_review_rating", "_dist_km", "_provincia", "_bayesian"
    }

    display_name = job_name or f"Job {job_id[:8]}"
    now_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    cards_html_parts = []
    for r in processed_rows:
        dist = r.get("_dist_km", 9999.99)
        dist_str = f"{dist} km" if dist != 9999.99 else "N/D"
        prov = str(r.get("_provincia", ""))

        name = str(r.get("title", ""))
        address = str(r.get("address", ""))
        phone = str(r.get("phone", ""))
        email = str(r.get("emails", ""))
        first_email = email.split(",")[0].strip() if email else ""

        count = int(r.get("_review_count", 0))
        rating = float(r.get("_review_rating", 0.0))
        bayes_score = float(r.get("_bayesian", 0.0))

        formatted_hours = format_open_hours(r.get("open_hours", ""))
        formatted_reviews = format_reviews(r.get("user_reviews", ""))

        category = str(r.get("category", ""))
        website = str(r.get("website", "")).strip()
        website_clean = website if website and website != "None" else ""
        website_display = website_clean.replace("https://", "").replace("http://", "").rstrip("/")
        if len(website_display) > 28:
            website_display = website_display[:28] + "…"
        website_link_html = f'<a href="{website_clean}" target="_blank" onclick="event.stopPropagation();" style="color:var(--accent-cyan);text-decoration:none;font-weight:500;" title="{website_clean}">🌐 {website_display}</a>' if website_clean else '<span style="color:var(--text-tertiary);">—</span>' 
        lat = str(r.get("latitude", ""))
        lng = str(r.get("longitude", ""))
        place_id = str(r.get("place_id", ""))

        if lat and lng and place_id:
            maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}&query_place_id={place_id}"
        elif name and address:
            query = urllib.parse.quote(f"{name} {address}")
            maps_url = f"https://www.google.com/maps/search/?api=1&query={query}"
        else:
            maps_url = "#"

        all_fields_html = "<table class='all-fields-table'>"
        for col, val in r.items():
            if col not in EXCLUDED_COLS and val:
                val_str = str(val)
                display_val = val_str[:800] + "..." if len(val_str) > 800 else val_str
                all_fields_html += f"<tr><th>{col}</th><td>{display_val}</td></tr>"
        all_fields_html += "</table>"

        cards_html_parts.append(f"""
        <details class="card-row" data-lat="{lat}" data-lng="{lng}" data-rating="{rating}" data-count="{count}" data-distance="{dist}" data-provincia="{prov}" data-bayes="{bayes_score}" data-email="{first_email}" data-website="{website_clean}" data-phone="{phone}">
            <summary>
                <div class="col-dist font-mono">{dist_str}</div>
                <div class="truncate">
                    <a href="{maps_url}" target="_blank" onclick="event.stopPropagation();" class="maps-link" title="Apri su Google Maps: {name}">📍 {name}</a>
                </div>
                <div class="truncate text-muted" title="{address}">{address}</div>
                <div class="truncate font-mono text-muted">{phone if phone else '—'}</div>
                <div class="truncate" title="{website_clean}">{website_link_html}</div>
                <div class="truncate" title="{email}"><a href="mailto:{first_email}" onclick="event.stopPropagation();" class="email-link">{first_email if first_email else '—'}</a></div>
                <div class="col-badge font-mono">{count}</div>
                <div class="col-rating font-mono">★ {rating:.1f}</div>
            </summary>
            
            <div class="expanded-content">
                <div class="detail-group">
                    <span class="detail-label">Affidabilità Statistica (Bayes)</span>
                    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
                        <span style="font-size:1.4rem;font-weight:800;color:#38bdf8;font-family:ui-monospace, monospace;">{bayes_score:.3f}</span>
                        <div style="flex:1;">
                            <div style="height:6px;background:#2a323d;border-radius:3px;overflow:hidden;">
                                <div style="height:100%;width:{min(100, max(0, (bayes_score/5.0)*100)):.1f}%;background:linear-gradient(90deg, #5c7c8a, #82a1b0);border-radius:3px;"></div>
                            </div>
                            <span style="font-size:11px;color:#64748b;">Scala normalizzata su {count} recensioni (Media voto: {rating})</span>
                        </div>
                    </div>
                </div>

                <div class="detail-group">
                    <span class="detail-label">Identità & Coordinate</span>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12.5px;">
                        <div><span style="color:#64748b;">Categoria:</span> <strong style="color:#e2e8f0;">{category if category else 'Non definita'}</strong></div>
                        <div><span style="color:#64748b;">Provincia:</span> <strong style="color:#e2e8f0;">{prov}</strong></div>
                        <div><span style="color:#64748b;">Lat, Lon:</span> <span class="font-mono" style="color:#94a3b8;">{lat[:8]}, {lng[:8]}</span></div>
                        <div><span style="color:#64748b;">Place ID:</span> <span class="font-mono" style="color:#64748b;font-size:11px;" title="{place_id}">{place_id[:12]}…</span></div>
                    </div>
                    {f'<div style="margin-top:6px;font-size:12px;"><span style="color:#64748b;">Tutte le email:</span> <span style="color:#34d399;font-family:ui-monospace,monospace;">{email}</span></div>' if email else ''}
                </div>
                
                <div class="detail-group">
                    <span class="detail-label">Orari Settimanali</span>
                    <div>{formatted_hours}</div>
                </div>

                <div class="detail-group" style="grid-column: 1 / -1;">
                    <span class="detail-label">Recensioni degli Utenti</span>
                    {formatted_reviews}
                </div>

                <div class="detail-group" style="grid-column: 1 / -1;background:rgba(15,18,22,0.4);border:1px dashed #2a323d;">
                    <details>
                        <summary style="font-weight:600;color:#94a3b8;cursor:pointer;padding:6px;font-size:12px;">🔽 Mostra tutti i rimanenti dati estratti ({len(r)} campi grezzi)</summary>
                        <div style="margin-top:10px;overflow-x:auto;">
                            {all_fields_html}
                        </div>
                    </details>
                </div>
            </div>
        </details>""")

    all_cards_str = "".join(cards_html_parts)

    html_content = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Report Studio - {display_name}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
    :root {{
        --bg-base: #111419;
        --bg-surface: #171b22;
        --bg-surface-hover: #1e232c;
        --border-subtle: #262c37;
        --border-focus: #0ea5e9;
        --text-primary: #eef2f6;
        --text-secondary: #9ba7b7;
        --text-tertiary: #626e80;
        --accent-blue: #0ea5e9;
        --accent-cyan: #38bdf8;
        --accent-emerald: #10b981;
        --accent-amber: #f59e0b;
        --accent-rose: #f43f5e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
        font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background-color: var(--bg-base);
        color: var(--text-primary);
        margin: 0;
        padding: 24px;
        line-height: 1.5;
        font-size: 13.5px;
    }}
    .report-container {{ max-width: 1560px; margin: 0 auto; }}
    
    .font-mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    .text-muted {{ color: var(--text-secondary); }}
    
    /* Toolbar Studio */
    .toolbar {{
        background: var(--bg-surface);
        border: 1px solid var(--border-subtle);
        padding: 16px 20px;
        border-radius: 8px;
        margin-bottom: 16px;
        display: flex;
        flex-direction: column;
        gap: 16px;
    }}
    .toolbar-header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px solid var(--border-subtle);
        padding-bottom: 12px;
        flex-wrap: wrap;
        gap: 12px;
    }}
    .toolbar-header h2 {{
        margin: 0;
        font-family: 'Space Grotesk', -apple-system, sans-serif;
        font-size: 1.3rem;
        font-weight: 700;
        letter-spacing: -0.01em;
        color: var(--text-primary);
        display: flex;
        align-items: center;
        gap: 10px;
    }}
    .toolbar-meta {{
        font-size: 12px;
        color: var(--text-secondary);
        display: flex;
        gap: 16px;
        flex-wrap: wrap;
        align-items: center;
    }}
    .toolbar-meta strong {{ color: var(--text-primary); }}
    
    .toolbar-controls {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 14px;
    }}
    
    .filters-group, .sort-group {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    
    .input-container {{
        display: flex;
        align-items: center;
        gap: 8px;
        background: #111419;
        padding: 6px 12px;
        border-radius: 6px;
        border: 1px solid var(--border-subtle);
        transition: border-color 0.15s;
    }}
    .input-container:focus-within {{ border-color: var(--border-focus); }}
    .input-container label {{
        font-size: 12px;
        font-weight: 600;
        color: var(--text-secondary);
        white-space: nowrap;
    }}
    .input-container input {{
        background: transparent;
        border: none;
        color: var(--text-primary);
        font-family: ui-monospace, monospace;
        font-size: 12.5px;
        outline: none;
        width: 65px;
        text-align: center;
    }}
    
    select {{
        background: #111419;
        color: var(--text-primary);
        border: 1px solid var(--border-subtle);
        padding: 7px 12px;
        border-radius: 6px;
        font-size: 12.5px;
        outline: none;
        cursor: pointer;
    }}
    select:focus {{ border-color: var(--border-focus); }}
    
    .btn {{
        background: #222834;
        color: var(--text-primary);
        border: 1px solid var(--border-subtle);
        padding: 6px 13px;
        border-radius: 6px;
        cursor: pointer;
        font-weight: 600;
        font-size: 12px;
        transition: all 0.15s;
        white-space: nowrap;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        text-decoration: none;
    }}
    .btn:hover {{ background: #2c3444; border-color: var(--border-focus); }}
    .btn-primary {{ background: var(--accent-blue); color: #fff; border-color: #0284c7; }}
    .btn-primary:hover {{ background: #0284c7; }}
    .btn-bayes {{ background: rgba(14, 165, 233, 0.12); color: #38bdf8; border-color: rgba(14, 165, 233, 0.35); }}
    .btn-bayes:hover {{ background: rgba(14, 165, 233, 0.22); color: #fff; }}
    
    /* Table Headers & Rows */
    .table-header, .card-row summary {{
        display: grid;
        grid-template-columns: 85px 2fr 2fr 1.3fr 1.8fr 1.5fr 75px 75px;
        gap: 12px;
        padding: 10px 16px;
        align-items: center;
    }}
    .table-header {{
        font-weight: 700;
        color: var(--text-secondary);
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        background: #13171d;
        border-radius: 8px 8px 0 0;
        border: 1px solid var(--border-subtle);
        border-bottom: none;
    }}
    
    .card-row {{
        background: var(--bg-surface);
        border: 1px solid var(--border-subtle);
        margin-bottom: 4px;
        border-radius: 6px;
        transition: background-color 0.15s, border-color 0.15s;
    }}
    .card-row[open] {{
        border-color: #38bdf8;
        background: #1b2129;
    }}
    .card-row summary {{
        cursor: pointer;
        font-size: 13px;
        list-style: none;
        user-select: none;
    }}
    .card-row summary:hover {{ background-color: var(--bg-surface-hover); }}
    .card-row summary::-webkit-details-marker {{ display: none; }}
    .truncate {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    
    .col-dist {{ font-weight: 700; color: #fb7185; font-size: 12px; }}
    .col-badge {{
        background: #242c37;
        color: var(--text-primary);
        padding: 3px 8px;
        border-radius: 4px;
        text-align: center;
        font-weight: 700;
        font-size: 12px;
    }}
    .col-rating {{
        background: rgba(245, 158, 11, 0.15);
        color: #fbbf24;
        border: 1px solid rgba(245, 158, 11, 0.3);
        padding: 3px 8px;
        border-radius: 4px;
        text-align: center;
        font-weight: 700;
        font-size: 12px;
    }}
    
    .maps-link {{
        color: var(--text-primary);
        font-weight: 600;
        text-decoration: none;
        transition: color 0.15s;
    }}
    .maps-link:hover {{ color: var(--accent-cyan); text-decoration: underline; }}
    
    .email-link {{
        color: var(--accent-emerald);
        text-decoration: none;
    }}
    .email-link:hover {{ text-decoration: underline; }}

    /* Expanded Details */
    .expanded-content {{
        border-top: 1px solid var(--border-subtle);
        padding: 20px;
        background-color: #12161c;
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
        gap: 16px;
        font-size: 13px;
    }}
    .detail-group {{
        display: flex;
        flex-direction: column;
        background: var(--bg-surface);
        padding: 14px 16px;
        border-radius: 6px;
        border: 1px solid var(--border-subtle);
    }}
    .detail-label {{
        font-weight: 700;
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        font-size: 10.5px;
        margin-bottom: 10px;
        border-bottom: 1px solid var(--border-subtle);
        padding-bottom: 5px;
    }}
    
    .reviews-container {{
        display: flex;
        flex-direction: column;
        gap: 10px;
        max-height: 420px;
        overflow-y: auto;
        padding-right: 6px;
    }}
    .review-card {{
        background: #14181f;
        border: 1px solid var(--border-subtle);
        border-radius: 6px;
        padding: 12px;
    }}
    .review-header {{ display: flex; align-items: flex-start; gap: 12px; margin-bottom: 8px; }}
    .review-avatar {{ width: 34px; height: 34px; border-radius: 50%; object-fit: cover; border: 1px solid var(--border-subtle); }}
    .review-avatar-placeholder {{ width: 34px; height: 34px; border-radius: 50%; background: #2a323d; }}
    .review-stars {{ color: #fbbf24; font-size: 13px; }}
    .review-body {{ font-style: normal; color: var(--text-secondary); line-height: 1.5; font-size: 12.5px; }}

    .all-fields-table {{
        width: 100%;
        border-collapse: collapse;
        margin-top: 8px;
        font-family: ui-monospace, monospace;
        font-size: 11.5px;
    }}
    .all-fields-table th, .all-fields-table td {{
        text-align: left;
        padding: 6px 10px;
        border-bottom: 1px solid var(--border-subtle);
    }}
    .all-fields-table th {{ width: 220px; color: var(--accent-cyan); font-weight: 600; }}
    .all-fields-table td {{ color: var(--text-secondary); word-break: break-word; }}
</style>
</head>
<body>
<div class="report-container">
    <div class="toolbar">
        <div class="toolbar-header">
            <div>
                <h2 id="last-update">📊 {display_name}</h2>
                <div class="toolbar-meta">
                    <span>Baricentro Attuale: <strong id="current-center-display" class="font-mono">{start_lat:.4f}, {start_lng:.4f}</strong></span>
                    <span>Modello Bayesiano: <strong class="font-mono">m={global_m:.2f}, C={global_c:.0f}</strong></span>
                    <span>Totale: <strong class="font-mono" style="color:var(--text-primary);">{len(processed_rows)}</strong></span>
                    <span>Filtrati: <strong id="filtered-count" class="font-mono" style="color:var(--accent-cyan);">{len(processed_rows)}</strong> <span id="filtered-ratio" style="font-size:12px;color:var(--text-tertiary);">({len(processed_rows)} su {len(processed_rows)})</span></span>
                </div>
            </div>
            <div style="display:flex; gap:8px;">
                <a href="/" class="btn">⬅ Dashboard Principale</a>
            </div>
        </div>
        
        <!-- Sezione Personalizzazione Baricentro in tempo reale -->
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:rgba(15,23,42,0.6);border:1px solid var(--border-subtle);padding:10px 14px;border-radius:6px;margin-bottom:12px;">
            <span style="font-size:12px;font-weight:700;color:var(--accent-cyan);display:inline-flex;align-items:center;gap:4px;">
                🎯 Modifica Baricentro:
            </span>
            <div style="display:inline-flex;align-items:center;gap:6px;">
                <label style="font-size:11px;color:var(--text-secondary);">Lat:</label>
                <input type="number" id="center-lat-input" step="0.0001" value="{start_lat:.4f}" style="width:90px;padding:4px 8px;background:var(--bg-base);border:1px solid var(--border-subtle);border-radius:4px;color:var(--text-primary);font-size:12px;font-family:ui-monospace,monospace;">
            </div>
            <div style="display:inline-flex;align-items:center;gap:6px;">
                <label style="font-size:11px;color:var(--text-secondary);">Lon:</label>
                <input type="number" id="center-lng-input" step="0.0001" value="{start_lng:.4f}" style="width:90px;padding:4px 8px;background:var(--bg-base);border:1px solid var(--border-subtle);border-radius:4px;color:var(--text-primary);font-size:12px;font-family:ui-monospace,monospace;">
            </div>
            <button type="button" class="btn" onclick="updateCenterFromInputs()" style="padding:4px 12px;font-size:12px;background:var(--accent-blue);color:#fff;">
                Ricalcola Distanze
            </button>
            <div style="display:inline-flex;gap:6px;align-items:center;margin-left:auto;">
                <span style="font-size:11px;color:var(--text-tertiary);">Preset rapidi:</span>
                <button type="button" class="btn" onclick="setCenterPreset(45.4389958, 10.9943574, 'Arena di Verona')" style="padding:3px 8px;font-size:11px;background:#1e293b;border:1px solid #334155;">
                    🏛️ Arena di Verona
                </button>
                <button type="button" class="btn" onclick="setCenterPreset(45.4287, 10.9823, 'Stazione Verona Porta Nuova')" style="padding:3px 8px;font-size:11px;background:#1e293b;border:1px solid #334155;">
                    🚆 Verona Porta Nuova
                </button>
                <button type="button" class="btn" onclick="setCenterPreset(45.4435, 10.9976, 'Piazza Erbe')" style="padding:3px 8px;font-size:11px;background:#1e293b;border:1px solid #334155;">
                    📍 Piazza Erbe
                </button>
            </div>
        </div>
        
        <div class="toolbar-controls">
            <!-- Gruppo Sinistra: Filtri -->
            <div class="filters-group">
                <select id="provincia-filter" onchange="applyFilters()">
                    <option value="all">Tutte le Province</option>
                    {prov_options}
                </select>

                <select id="has-website-filter" onchange="applyFilters()" title="Filtra per presenza del sito web">
                    <option value="all">🌐 Sito: Tutti</option>
                    <option value="yes">Con sito web</option>
                    <option value="no">Senza sito web</option>
                </select>

                <select id="has-email-filter" onchange="applyFilters()" title="Filtra per presenza dell'email">
                    <option value="all">📧 Email: Tutte</option>
                    <option value="yes">Con email</option>
                    <option value="no">Senza email</option>
                </select>

                <select id="has-phone-filter" onchange="applyFilters()" title="Filtra per presenza del numero di telefono">
                    <option value="all">📞 Tel: Tutti</option>
                    <option value="yes">Con telefono</option>
                    <option value="no">Senza telefono</option>
                </select>
                
                <div class="input-container" title="Distanza massima in km">
                    <label for="dist-input">Max Dist (km):</label>
                    <input type="number" id="dist-input" placeholder="∞" oninput="applyFilters()" onkeydown="handleEnter(event)">
                </div>

                <div class="input-container" title="Numero minimo di recensioni">
                    <label for="min-rev-input">Min Recensioni:</label>
                    <input type="number" id="min-rev-input" placeholder="0" oninput="applyFilters()" onkeydown="handleEnter(event)">
                </div>

                <div class="input-container" title="Media voto minima (es. 4.5)">
                    <label for="min-rating-input">Min Rating ⭐:</label>
                    <input type="number" id="min-rating-input" step="0.1" placeholder="0.0" oninput="applyFilters()" onkeydown="handleEnter(event)">
                </div>
            </div>

            <!-- Gruppo Destra: Ordinamento e Azioni -->
            <div class="sort-group">
                <button class="btn btn-primary" onclick="exportEmails()">📧 Esporta Email</button>
                <button class="btn" style="background:var(--accent-blue);color:#fff;" onclick="exportWebsites()">🌐 Esporta Siti Web</button>
                <button class="btn btn-bayes" onclick="sortBy('bayesian')">📈 Ordina Affidabilità (Bayes)</button>
                <button class="btn" onclick="sortBy('distance')">📍 Ordina per Distanza</button>
            </div>
        </div>
    </div>
    
    <div class="table-header">
        <div>Dist.</div>
        <div>Nome (Apri Maps)</div>
        <div>Indirizzo</div>
        <div>Telefono</div>
        <div>Sito Web</div>
        <div>Email</div>
        <div style="text-align: center;">Rec.</div>
        <div style="text-align: center;">Media</div>
    </div>
    <div id="cards-container">
        {all_cards_str}
    </div>
</div>
<script>
    let currentSortMode = 'bayesian'; 

    function handleEnter(event) {{
        if (event.key === 'Enter') {{
            applyFilters();
            event.target.blur();
        }}
    }}

    function applyFilters() {{
        updateLayout(currentSortMode);
    }}

    function sortBy(mode) {{
        currentSortMode = mode;
        updateLayout(mode);
    }}

    function updateLayout(mode) {{
        const container = document.getElementById('cards-container');
        const cards = Array.from(container.getElementsByClassName('card-row'));
        
        const provFilter = document.getElementById('provincia-filter').value;
        const websiteFilter = document.getElementById('has-website-filter').value;
        const emailFilter = document.getElementById('has-email-filter').value;
        const phoneFilter = document.getElementById('has-phone-filter').value;
        
        const distInputVal = document.getElementById('dist-input').value.trim();
        const maxDist = (distInputVal === "" || isNaN(distInputVal)) ? Infinity : parseFloat(distInputVal);

        const revInputVal = document.getElementById('min-rev-input').value.trim();
        const minRev = (revInputVal === "" || isNaN(revInputVal)) ? 0 : parseInt(revInputVal);

        const ratingInputVal = document.getElementById('min-rating-input').value.trim();
        const minRating = (ratingInputVal === "" || isNaN(ratingInputVal)) ? 0 : parseFloat(ratingInputVal);

        cards.sort((a, b) => {{
            const distA = parseFloat(a.getAttribute('data-distance'));
            const distB = parseFloat(b.getAttribute('data-distance'));
            const bayesA = parseFloat(a.getAttribute('data-bayes'));
            const bayesB = parseFloat(b.getAttribute('data-bayes'));

            if (mode === 'distance') {{
                return distA - distB;
            }} 
            else if (mode === 'bayesian') {{
                return bayesB - bayesA; 
            }}
        }});
        
        let visibleCount = 0;
        cards.forEach(card => {{
            const cardProv = card.getAttribute('data-provincia');
            const cardDist = parseFloat(card.getAttribute('data-distance'));
            const cardRev = parseInt(card.getAttribute('data-count'));
            const cardRating = parseFloat(card.getAttribute('data-rating'));
            const cardEmail = (card.getAttribute('data-email') || '').trim();
            const cardWebsite = (card.getAttribute('data-website') || '').trim();
            const cardPhone = (card.getAttribute('data-phone') || '').trim();
            
            const isProvMatch = (provFilter === 'all' || cardProv === provFilter);
            const isDistMatch = (cardDist <= maxDist);
            const isRevMatch = (cardRev >= minRev);
            const isRatingMatch = (cardRating >= minRating);

            const hasWebsite = (cardWebsite !== '' && cardWebsite !== 'None');
            const isWebsiteMatch = (websiteFilter === 'all' || (websiteFilter === 'yes' && hasWebsite) || (websiteFilter === 'no' && !hasWebsite));

            const hasEmail = (cardEmail !== '' && cardEmail !== 'None');
            const isEmailMatch = (emailFilter === 'all' || (emailFilter === 'yes' && hasEmail) || (emailFilter === 'no' && !hasEmail));

            const hasPhone = (cardPhone !== '' && cardPhone !== 'None');
            const isPhoneMatch = (phoneFilter === 'all' || (phoneFilter === 'yes' && hasPhone) || (phoneFilter === 'no' && !hasPhone));
            
            if (isProvMatch && isDistMatch && isRevMatch && isRatingMatch && isWebsiteMatch && isEmailMatch && isPhoneMatch) {{
                card.style.display = '';
                visibleCount++;
            }} else {{
                card.style.display = 'none';
            }}
            
            container.appendChild(card);
        }});

        const filteredElem = document.getElementById('filtered-count');
        const ratioElem = document.getElementById('filtered-ratio');
        if (filteredElem) {{
            filteredElem.textContent = visibleCount;
        }}
        if (ratioElem) {{
            ratioElem.textContent = "(" + visibleCount + " su " + cards.length + ")";
        }}
    }}

    function exportEmails() {{
        const container = document.getElementById('cards-container');
        const cards = Array.from(container.getElementsByClassName('card-row'));
        let emailsToExport = [];

        cards.forEach(card => {{
            if (card.style.display !== 'none') {{
                const email = card.getAttribute('data-email');
                if (email && email.trim() !== '') {{
                    emailsToExport.push(email.trim());
                }}
            }}
        }});

        if (emailsToExport.length === 0) {{
            alert("Nessuna email trovata nei profili attualmente filtrati.");
            return;
        }}

        const textContent = emailsToExport.join(String.fromCharCode(10));
        const blob = new Blob([textContent], {{ type: 'text/plain' }});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        
        link.href = url;
        link.download = 'email_filtrate.txt';
        
        document.body.appendChild(link);
        link.click();
        
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }}

    function exportWebsites() {{
        const container = document.getElementById('cards-container');
        const cards = Array.from(container.getElementsByClassName('card-row'));
        let websitesToExport = [];

        cards.forEach(card => {{
            if (card.style.display !== 'none') {{
                const ws = card.getAttribute('data-website');
                if (ws && ws.trim() !== '' && ws !== 'None') {{
                    websitesToExport.push(ws.trim());
                }}
            }}
        }});

        if (websitesToExport.length === 0) {{
            alert("Nessun sito web trovato nei profili attualmente filtrati.");
            return;
        }}

        const uniqueWebsites = Array.from(new Set(websitesToExport));
        const textContent = uniqueWebsites.join(String.fromCharCode(10));
        const blob = new Blob([textContent], {{ type: 'text/plain' }});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        
        link.href = url;
        link.download = 'siti_web_filtrati.txt';
        
        document.body.appendChild(link);
        link.click();
        
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }}

    // ---- Client-side Distance Calculation & Dynamic Centroid ----
    function calculateDistanceClient(lat1, lon1, lat2, lon2) {{
        try {{
            const R = 6371.0;
            const toRad = Math.PI / 180;
            const dLat = (lat2 - lat1) * toRad;
            const dLon = (lon2 - lon1) * toRad;
            const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                      Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) *
                      Math.sin(dLon / 2) * Math.sin(dLon / 2);
            const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
            return Math.round((R * c) * 100) / 100;
        }} catch (e) {{
            return 9999.99;
        }}
    }}

    function setCenterPreset(lat, lng, label) {{
        document.getElementById('center-lat-input').value = lat.toFixed(4);
        document.getElementById('center-lng-input').value = lng.toFixed(4);
        recalculateAllDistances(lat, lng, label);
    }}

    function updateCenterFromInputs() {{
        const lat = parseFloat(document.getElementById('center-lat-input').value);
        const lng = parseFloat(document.getElementById('center-lng-input').value);
        if (isNaN(lat) || isNaN(lng)) {{
            alert('Coordinate non valide. Inserisci latitudine e longitudine numeriche.');
            return;
        }}
        recalculateAllDistances(lat, lng);
    }}

    function recalculateAllDistances(centerLat, centerLng, label) {{
        const container = document.getElementById('cards-container');
        const cards = Array.from(container.getElementsByClassName('card-row'));

        const displayEl = document.getElementById('current-center-display');
        if (displayEl) {{
            displayEl.textContent = label ? `${{centerLat.toFixed(4)}}, ${{centerLng.toFixed(4)}} (${{label}})` : `${{centerLat.toFixed(4)}}, ${{centerLng.toFixed(4)}}`;
        }}

        cards.forEach(card => {{
            const lat = parseFloat(card.getAttribute('data-lat'));
            const lng = parseFloat(card.getAttribute('data-lng'));
            let dist = 9999.99;
            if (!isNaN(lat) && !isNaN(lng) && (lat !== 0 || lng !== 0)) {{
                dist = calculateDistanceClient(centerLat, centerLng, lat, lng);
            }}
            card.setAttribute('data-distance', dist);

            const distElem = card.querySelector('.col-dist');
            if (distElem) {{
                distElem.textContent = dist !== 9999.99 ? `${{dist.toFixed(2)}} km` : 'N/D';
            }}
        }});

        // Update URL query parameters without reloading page
        const newUrl = new URL(window.location);
        newUrl.searchParams.set('lat', centerLat);
        newUrl.searchParams.set('lng', centerLng);
        window.history.replaceState({{}}, '', newUrl);

        applyFilters();
    }}

    window.onload = function() {{
        sortBy('bayesian'); 
    }};
</script>
</body>
</html>
"""
    return html_content

@app.route("/jobs/<job_id>/report", methods=["GET"])
@app.route("/api/v1/jobs/<job_id>/report", methods=["GET"])
def job_report_view(job_id):
    try:
        # 1. Fetch CSV data for job
        local_csv = os.path.join(DATA_DIR, f"{job_id}.csv")
        if os.path.exists(local_csv):
            with open(local_csv, "r", encoding="utf-8", errors="replace") as f:
                csv_text = f.read()
        else:
            req = urllib.request.Request(f"{BACKEND_URL}/download?id={job_id}")
            with urllib.request.urlopen(req) as resp:
                csv_text = resp.read().decode("utf-8", errors="replace")

        # 2. Try to fetch job name and geo parameters from SQLite jobs.db
        job_name = f"Job {job_id[:8]}"
        job_geo_lat = None
        job_geo_lng = None
        try:
            db_path = os.path.join(DATA_DIR, "jobs.db")
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT name, data FROM jobs WHERE id=?", (job_id,))
                row = c.fetchone()
                if row:
                    if row["name"]:
                        job_name = row["name"]
                    if row["data"]:
                        try:
                            jd = json.loads(row["data"])
                            # Check lat/lon in job search parameters (-geo)
                            lat_candidate = jd.get("lat")
                            lon_candidate = jd.get("lon") or jd.get("lng")
                            if lat_candidate and lon_candidate:
                                lat_flt = float(lat_candidate)
                                lon_flt = float(lon_candidate)
                                if lat_flt != 0.0 or lon_flt != 0.0:
                                    job_geo_lat = lat_flt
                                    job_geo_lng = lon_flt
                        except Exception:
                            pass
                conn.close()
        except Exception:
            pass

        user_lat = request.args.get("lat", None)
        user_lng = request.args.get("lng", None)

        html_out = render_job_report_html(
            job_id,
            job_name,
            csv_text,
            user_lat=user_lat,
            user_lng=user_lng,
            job_geo_lat=job_geo_lat,
            job_geo_lng=job_geo_lng
        )
        return Response(html_out, mimetype="text/html")
    except Exception as e:
        return Response(f"Errore generazione report: {e}", status=500)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
