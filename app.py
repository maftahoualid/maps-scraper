import os
import io
import csv
import json
import math
import time
import socket
import threading
import http.client
import sqlite3
import urllib.request
import urllib.error
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

def deduplicate_and_merge_csv(target_csv_path, source_csv_content):
    """
    Appends new rows from source_csv_content to target_csv_path,
    skipping rows that have already been recorded based on link, place_id, or title+address.
    """
    existing_identities = set()
    file_exists = os.path.exists(target_csv_path) and os.path.getsize(target_csv_path) > 0

    if file_exists:
        try:
            with open(target_csv_path, mode="r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
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
    fieldnames = new_reader.fieldnames or []
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

    # Write or append
    mode = "a" if file_exists else "w"
    with open(target_csv_path, mode=mode, encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for r in rows_to_append:
            writer.writerow(r)

    return len(rows_to_append)

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
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
    <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
    <style>
:root {
    --color-background: #0f172a;
    --color-surface: #1e293b;
    --color-surface-hover: #334155;
    --color-text: #f1f5f9;
    --color-text-light: #94a3b8;
    --color-border: #334155;
    --color-primary: #3b82f6;
    --color-primary-light: #60a5fa;
    --color-success: #10b981;
    --color-warning: #f59e0b;
    --color-error: #ef4444;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen-Sans, Ubuntu, Cantarell, 'Helvetica Neue', sans-serif;
    line-height: 1.5;
    color: var(--color-text);
    background-color: var(--color-background);
    margin: 0;
    padding: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
}

.top-navbar {
    height: 56px;
    background-color: var(--color-surface);
    border-bottom: 1px solid var(--color-border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
    z-index: 100;
}

.top-navbar .navbar-brand {
    display: flex;
    align-items: center;
    gap: 12px;
}

.top-navbar .brand-icon {
    font-size: 1.5rem;
}

.top-navbar .brand-title {
    font-size: 1.25rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    background: linear-gradient(135deg, #60a5fa 0%, #a78bfa 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}

.top-navbar .brand-badge {
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 9999px;
    background-color: rgba(59, 130, 246, 0.2);
    color: var(--color-primary-light);
    border: 1px solid rgba(59, 130, 246, 0.3);
    font-weight: 600;
}

.top-navbar-right {
    display: flex;
    align-items: center;
    gap: 16px;
}

.nav-tabs {
    display: flex;
    align-items: center;
    gap: 6px;
    background-color: rgba(15, 23, 42, 0.7);
    padding: 4px;
    border-radius: 8px;
    border: 1px solid var(--color-border);
}

.nav-tab-btn {
    padding: 6px 14px;
    font-size: 13.5px;
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
    background-color: rgba(255, 255, 255, 0.05);
}

.nav-tab-btn.active {
    background-color: var(--color-primary);
    color: #ffffff;
    box-shadow: 0 2px 6px rgba(59, 130, 246, 0.4);
}

.tab-badge {
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 999px;
    background: rgba(0, 0, 0, 0.3);
    color: inherit;
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
    font-size: 1.6rem;
    font-weight: 700;
    color: var(--color-text);
}

.search-header-box p {
    margin: 0;
    color: var(--color-text-light);
    font-size: 14px;
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
    background: rgba(30, 41, 59, 0.5);
    border: 1px dashed var(--color-border);
    border-radius: 12px;
    margin-top: 20px;
}

.empty-results-box .empty-icon {
    font-size: 48px;
    margin-bottom: 14px;
}

.empty-results-box h3 {
    margin: 0 0 8px 0;
    font-size: 1.25rem;
    color: var(--color-text);
}

.empty-results-box p {
    margin: 0 0 20px 0;
    color: var(--color-text-light);
    font-size: 14px;
}

.app-container {
    flex: 1;
    display: flex;
    flex-direction: column;
    height: 100vh;
}

main {
    flex: 1;
    display: flex;
    overflow: hidden;
}

.sidebar {
    width: 380px;
    background-color: var(--color-surface);
    padding: 24px;
    overflow-y: auto;
    border-right: 1px solid var(--color-border);
    box-shadow: 2px 0 10px rgba(0, 0, 0, 0.3);
}

.content {
    flex: 1;
    padding: 24px 32px;
    overflow-y: auto;
    background-color: var(--color-background);
}

.table-header-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
}

.table-title {
    font-size: 1.1rem;
    font-weight: 600;
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
    padding: 7px 14px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: all 0.2s;
}

.btn-engine-pause {
    background-color: #f59e0b;
    color: #0f172a;
}
.btn-engine-pause:hover { background-color: #fbbf24; }

.btn-engine-resume {
    background-color: #10b981;
    color: #0f172a;
}
.btn-engine-resume:hover { background-color: #34d399; }

.badge-engine {
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
}
.badge-engine-running { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
.badge-engine-paused { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }

form {
    display: flex;
    flex-direction: column;
    gap: 18px;
}

fieldset {
    border: none;
    padding: 0;
    margin: 0;
}

legend {
    font-weight: 600;
    margin-bottom: 12px;
    color: var(--color-text);
    font-size: 15px;
}

.form-group {
    margin-bottom: 14px;
}

label {
    display: block;
    margin-bottom: 5px;
    color: var(--color-text-light);
    font-size: 13px;
}

input[type="text"],
input[type="number"],
textarea,
select {
    width: 100%;
    padding: 8px 10px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    font-size: 13.5px;
    box-sizing: border-box;
    background: #0f172a;
    color: var(--color-text);
    transition: border-color 0.2s, box-shadow 0.2s;
}

input:focus, textarea:focus, select:focus {
    outline: none;
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
}

input[type="file"] {
    font-size: 12px;
    color: var(--color-text-light);
}

button {
    background-color: var(--color-primary);
    color: white;
    border: none;
    padding: 12px 20px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
    transition: background-color 0.2s, transform 0.05s;
}

button:hover { background-color: var(--color-primary-light); }
button:active { transform: scale(0.99); }
button:disabled { background-color: #475569; cursor: not-allowed; }

table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    background-color: var(--color-surface);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid var(--color-border);
}

th, td {
    text-align: left;
    padding: 14px 16px;
    border-bottom: 1px solid var(--color-border);
}

th {
    font-weight: 600;
    color: var(--color-text-light);
    background-color: rgba(15, 23, 42, 0.6);
}

tr:hover td { background-color: rgba(255, 255, 255, 0.02); }

.status-indicator {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 600;
    text-transform: lowercase;
}

.status-ok { background-color: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
.status-pending { background-color: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
.status-working { background-color: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); animation: pulse 1.5s infinite; }
.status-paused { background-color: rgba(245, 158, 11, 0.25); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.5); }
.status-failed { background-color: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
.status-error { background-color: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
}

.download-button, .delete-button, .view-button, .resume-button, .pause-job-button, .leads-button {
    padding: 5px 10px;
    border-radius: 4px;
    font-size: 12px;
    text-decoration: none;
    color: white;
    display: inline-flex;
    align-items: center;
    cursor: pointer;
    border: none;
    margin-right: 4px;
    transition: filter 0.15s;
}

.download-button:hover, .delete-button:hover, .view-button:hover, .resume-button:hover, .pause-job-button:hover, .leads-button:hover {
    filter: brightness(1.15);
}

.download-button { background-color: #059669; }
.view-button { background-color: #2563eb; }
.pause-job-button { background-color: #f59e0b; color: #0f172a; font-weight: 600; }
.resume-button { background-color: #10b981; color: #0f172a; font-weight: 600; }
.leads-button { background-color: #0284c7; }
.delete-button { background-color: #dc2626; }

.json-button {
    padding: 5px 10px;
    border-radius: 4px;
    font-size: 12px;
    text-decoration: none;
    color: white;
    background-color: #7c3aed;
    display: inline-flex;
    align-items: center;
    cursor: pointer;
    border: none;
    margin-right: 4px;
    transition: filter 0.15s;
}
.json-button:hover { filter: brightness(1.15); }

.error-message {
    display: none;
    background-color: rgba(239, 68, 68, 0.15);
    border: 1px solid rgba(239, 68, 68, 0.4);
    color: #fca5a5;
    padding: 12px 16px;
    border-radius: 6px;
    margin-bottom: 20px;
    font-size: 13.5px;
}

.error-message:not(:empty) {
    display: block;
}

.expandable-section {
    border-radius: 6px;
    overflow: hidden;
}

.expandable-section summary {
    cursor: pointer;
    padding: 10px 14px;
    background-color: rgba(15, 23, 42, 0.6);
    border: 1px solid var(--color-border);
    border-radius: 6px;
    color: var(--color-text);
    list-style: none;
    font-size: 13.5px;
    font-weight: 500;
    transition: background-color 0.15s;
}

.expandable-section summary:hover {
    background-color: rgba(51, 65, 85, 0.6);
}

.expandable-section summary::-webkit-details-marker {
    display: none;
}

.expandable-section[open] summary {
    border-bottom: none;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    background-color: rgba(51, 65, 85, 0.5);
}

.expandable-section[open] fieldset {
    border: 1px solid var(--color-border);
    border-top: none;
    border-radius: 0 0 6px 6px;
    padding: 14px;
    background: rgba(15, 23, 42, 0.4);
}

@media (max-width: 768px) {
    main { flex-direction: column; }
    .sidebar {
        width: 100%;
        border-right: none;
        border-bottom: 1px solid var(--color-border);
        box-shadow: 0 2px 5px rgba(0, 0, 0, 0.2);
    }
    .content { padding: 20px; }
}

.spinner {
    display: none;
    width: 20px;
    height: 20px;
    border: 3px solid #334155;
    border-top-color: var(--color-primary);
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
    background: rgba(0, 0, 0, 0.75);
    backdrop-filter: blur(4px);
    justify-content: center;
    align-items: center;
}

.map-modal-content {
    background: var(--color-surface);
    color: var(--color-text);
    width: 90vw;
    height: 85vh;
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--color-border);
    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
}

.map-modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--color-border);
    background: rgba(15, 23, 42, 0.6);
}

.map-modal-header h2 { margin: 0; font-size: 1.1rem; color: var(--color-text); }

.map-modal-close {
    background: none;
    border: none;
    font-size: 1.6rem;
    line-height: 1;
    cursor: pointer;
    color: var(--color-text-light);
    padding: 0;
}

.map-modal-close:hover { background: none; color: #fff; }

.map-modal-body { flex: 1; display: flex; min-height: 0; }

#job-map { flex: 3; height: 100%; }

#job-places-list {
    flex: 1;
    min-width: 260px;
    max-width: 380px;
    overflow-y: auto;
    border-left: 1px solid var(--color-border);
    padding: 10px;
    background: var(--color-surface);
}

.place-item {
    padding: 10px;
    border-bottom: 1px solid var(--color-border);
    cursor: pointer;
    border-radius: 4px;
    margin-bottom: 4px;
    transition: background-color 0.15s;
}
.place-item:hover { background: rgba(59, 130, 246, 0.15); }
.place-title { font-weight: 600; font-size: 13.5px; color: #f1f5f9; }
.place-address { font-size: 0.8rem; color: var(--color-text-light); }
.place-rating { font-size: 0.8rem; color: #fbbf24; }

.map-loading, .map-empty, .map-error { padding: 12px; color: var(--color-text-light); }
.map-error { color: #f87171; }

.form-group.checkbox {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin-bottom: 12px;
}

.form-group.checkbox label {
    margin-bottom: 0;
    cursor: pointer;
    color: var(--color-text);
}

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

.badge-flag { background: #334155; color: #cbd5e1; }

.text-muted {
    font-size: 12px;
    color: var(--color-text-light);
    line-height: 1.4;
    margin-top: 4px;
}

.actions-cell {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
}

.grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
}

/* Pro Mode Switch Styling */
.pro-toggle-switch {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: #1e293b;
    border: 1px solid var(--color-border);
    padding: 6px 12px;
    border-radius: 20px;
    cursor: pointer;
    user-select: none;
    transition: all 0.2s ease;
}
.pro-toggle-switch:hover {
    border-color: var(--color-primary);
}
.pro-toggle-switch input[type="checkbox"] {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
    pointer-events: none;
}
.pro-slider {
    position: relative;
    width: 36px;
    height: 20px;
    background-color: #475569;
    border-radius: 20px;
    transition: 0.3s;
    display: inline-block;
}
.pro-slider:before {
    position: absolute;
    content: "";
    height: 14px;
    width: 14px;
    left: 3px;
    bottom: 3px;
    background-color: white;
    border-radius: 50%;
    transition: 0.3s;
}
.pro-toggle-switch input:checked + .pro-slider {
    background-color: var(--color-primary);
}
.pro-toggle-switch input:checked + .pro-slider:before {
    transform: translateX(16px);
}
.pro-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-text);
    display: flex;
    align-items: center;
    gap: 4px;
}
.pro-toggle-switch input:checked ~ .pro-label {
    color: #60a5fa;
}

/* Pro Mode Field Visibility */
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
                    ⏸ Pause All Jobs
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
                                <input type="text" id="name" name="name" value="" placeholder="es. Ristoranti Milano" autofocus>
                            </div>
                            <div class="form-group">
                                <label for="keywords">Keywords / Queries: (one per line)</label>
                                <textarea id="keywords" name="keywords" rows="4" placeholder="ristoranti verona centro&#10;pizzerie verona"></textarea>
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
                                    <input type="text" id="geo" placeholder="e.g. 45.4415,10.9972" onchange="parseGeoInput(this.value)">
                                    <p class="text-muted"><small>Format: latitude,longitude</small></p>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="latitude">Latitude:</label>
                                        <input type="number" step="0.000000000000001" id="latitude" name="latitude" value="0">
                                    </div>
                                    <div class="form-group">
                                        <label for="longitude">Longitude:</label>
                                        <input type="number" step="0.000000000000001" id="longitude" name="longitude" value="0">
                                    </div>
                                </div>

                                <div class="grid-2">
                                    <div class="form-group">
                                        <label for="zoom">Zoom (0-21): <span class="badge-tag badge-flag">-zoom</span></label>
                                        <input type="number" id="zoom" name="zoom" value="15" min="0" max="21" oninput="updateZoomAreaEstimate()">
                                        <p class="text-muted" id="zoom-estimate" style="color:#60a5fa; font-weight:500; font-size:12px; margin-top:4px;">
                                            📐 Superficie coperta: ~1.5 - 6 km² (Livello Quartiere)
                                        </p>
                                    </div>
                                    <div class="form-group">
                                        <label for="radius">Radius (meters): <span class="badge-tag badge-flag">-radius</span></label>
                                        <input type="number" id="radius" name="radius" value="10000">
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
                                        <input type="number" step="1" id="depth" name="depth" value="10">
                                    </div>
                                    <div class="form-group">
                                        <label for="maxtime">Max job time:</label>
                                        <input type="text" id="maxtime" name="maxtime" value="10m">
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
                        <button type="button" class="btn-primary" onclick="switchView('search')" style="background-color:var(--color-primary); color:#fff; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-weight:600; font-size:13.5px; display:inline-flex; align-items:center; gap:6px;">
                            ➕ Nuova Ricerca
                        </button>
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
                                    <th>Job ID</th>
                                    <th>Job Name</th>
                                    <th>Job Date</th>
                                    <th>Status</th>
                                    <th>Actions</th>
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
        if (paused) {
            badge.className = 'badge-engine badge-engine-paused';
            badge.textContent = 'Engine Paused';
            btn.className = 'btn-engine btn-engine-resume';
            btn.innerHTML = '▶ Resume All Jobs';
        } else {
            badge.className = 'badge-engine badge-engine-running';
            badge.textContent = 'Engine Active';
            btn.className = 'btn-engine btn-engine-pause';
            btn.innerHTML = '⏸ Pause All Jobs';
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
                actionsHtml += `<button class="pause-job-button" onclick="pauseJob('${j.id}')" title="Pause this search and save results">⏸ Pause</button>`;
            }

            // Resume button for paused, failed, or completed jobs to continue scraping missing places without duplicates
            if (status === 'paused' || status === 'failed' || status === 'error' || status === 'ok') {
                actionsHtml += `<button class="resume-button" onclick="resumeJobSearch('${j.id}')" title="Resume search from saved results without duplicates">▶ Resume</button>`;
            }

            // Export & View buttons
            if (status === 'ok' || status === 'paused' || status === 'working') {
                actionsHtml += `<button class="view-button" onclick="openMapModal('${j.id}', '${name}')">View</button>`;
                actionsHtml += `<a href="/api/v1/jobs/${j.id}/download?format=csv" class="download-button" title="Download CSV">CSV</a>`;
                actionsHtml += `<a href="/api/v1/jobs/${j.id}/download?format=json" class="json-button" title="Download JSON (-json)">JSON</a>`;
                actionsHtml += `<button class="leads-button" onclick="exportToLeadsDB('${j.id}')" title="Export to LeadsDB (-leadsdb-api-key)">LeadsDB</button>`;
            }

            actionsHtml += `<button class="delete-button" onclick="deleteJob('${j.id}')">Delete</button>`;
            actionsHtml += '</div>';

            const shortId = id.substring(0, 8);

            html += `<tr>
                <td title="${id}"><a href="/api/v1/jobs/${id}" target="_blank" style="color:var(--color-primary);text-decoration:none;" title="View Clean JSON details">${shortId}…</a></td>
                <td><strong>${name}</strong></td>
                <td>${date}</td>
                <td>${statusHtml}</td>
                <td>${actionsHtml}</td>
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

                const item = document.createElement('div');
                item.className = 'place-item';
                item.innerHTML = `
                    <div class="place-title">${escapeHtml(title)}</div>
                    ${address ? `<div class="place-address">${escapeHtml(address)}</div>` : ''}
                    ${phone ? `<div class="place-address">📞 ${escapeHtml(phone)}</div>` : ''}
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
                    const m = L.circleMarker([lat, lon], {
                        radius: 6,
                        fillColor: "#3b82f6",
                        color: "#ffffff",
                        weight: 1,
                        opacity: 1,
                        fillOpacity: 0.8
                    }).bindPopup(`<strong>${escapeHtml(title)}</strong><br>${escapeHtml(addr)}<br>${ph ? '📞 ' + escapeHtml(ph) : ''}`);
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
    2. Stops any running CLI runner container for this job.
    3. Results accumulated so far remain safely in <job_id>.csv on disk.
    """
    try:
        # Stop background runner container if one is running
        stop_job_runner(job_id)

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
            date_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["created_at"])) if r["created_at"] else ""
            max_time_raw = d.get("max_time", 0)
            max_time_sec = int(max_time_raw / 1_000_000_000) if max_time_raw > 1_000_000 else max_time_raw

            jobs.append({
                "id": r["id"],
                "name": r["name"],
                "date": date_str,
                "status": r["status"],
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
        req_data = json.dumps(cleaned_payload).encode("utf-8")
        req = urllib.request.Request(
            f"{BACKEND_URL}/api/v1/jobs",
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return jsonify(data), 201

    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        return Response(err_msg, status=e.code)
    except Exception as e:
        return Response(str(e), status=500)

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
        # Delete from backend
        try:
            req = urllib.request.Request(f"{BACKEND_URL}/delete?id={job_id}", method="DELETE")
            urllib.request.urlopen(req)
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
