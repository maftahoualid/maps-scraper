import os
import io
import csv
import json
import urllib.request
import urllib.error
from flask import Flask, render_template_string, request, jsonify, Response

app = Flask(__name__)
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8081")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Google Maps Scraper</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css" />
    <style>
:root {
    --color-background: #f9f9f9;
    --color-surface: #ffffff;
    --color-text: #333333;
    --color-text-light: #666666;
    --color-border: #e0e0e0;
    --color-primary: #4a4a4a;
    --color-primary-light: #5a5a5a;
    --color-success: #43a047;
    --color-warning: #fdd835;
    --color-error: #e53935;
    --color-github-star: #f1e05a;
    --color-sponsor: #4a4a4a;
    --color-sponsor-hover: #5a5a5a;
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

.app-container {
    flex: 1;
    display: flex;
    flex-direction: column;
}

header {
    background-color: var(--color-surface);
    padding: 24px 32px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
}

h1 {
    font-size: 24px;
    font-weight: 500;
    color: var(--color-primary);
    margin: 0 0 16px 0;
}

.github-section {
    display: flex;
    align-items: center;
    margin-top: 16px;
}

.github-button {
    display: inline-flex;
    align-items: center;
    background-color: var(--color-sponsor);
    color: white;
    border: 1px solid var(--color-border);
    padding: 10px 16px;
    margin-left: 16px;
    border-radius: 4px;
    font-size: 14px;
    text-decoration: none;
    transition: background-color 0.2s, border-color 0.2s;
}

.github-button:hover {
    background-color: var(--color-sponsor-hover);
    border-color: var(--color-primary);
}

.github-button svg {
    margin-right: 8px;
}

.github-button svg path {
    fill: var(--color-github-star);
}

main {
    flex: 1;
    display: flex;
    overflow: hidden;
}

.sidebar {
    width: 320px;
    background-color: var(--color-surface);
    padding: 24px;
    overflow-y: auto;
    border-right: 1px solid var(--color-border);
    box-shadow: 2px 0 5px rgba(0, 0, 0, 0.05);
}

.content {
    flex: 1;
    padding: 24px 32px;
    overflow-y: auto;
    background-color: var(--color-background);
}

form {
    display: flex;
    flex-direction: column;
    gap: 24px;
}

fieldset {
    border: none;
    padding: 0;
    margin: 0;
}

legend {
    font-weight: 500;
    margin-bottom: 16px;
    color: var(--color-primary);
}

.form-group {
    margin-bottom: 20px;
}

label {
    display: block;
    margin-bottom: 8px;
    color: var(--color-text-light);
}

input[type="text"],
input[type="number"],
textarea {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    font-size: 14px;
    box-sizing: border-box;
}

button {
    background-color: var(--color-primary);
    color: white;
    border: none;
    padding: 12px 20px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
    transition: background-color 0.2s;
}

button:hover {
    background-color: var(--color-primary-light);
}

button:disabled {
    background-color: #aaa;
    cursor: not-allowed;
}

table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    background-color: var(--color-surface);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    border-radius: 4px;
    overflow: hidden;
}

th, td {
    text-align: left;
    padding: 16px;
    border-bottom: 1px solid var(--color-border);
}

th {
    font-weight: 500;
    color: var(--color-primary);
    background-color: var(--color-surface);
}

.status-indicator {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
}

.status-ok {
    background-color: var(--color-success);
    color: white;
}

.status-pending {
    background-color: var(--color-warning);
    color: var(--color-text);
}

.status-working {
    background-color: var(--color-warning);
    color: var(--color-text);
}

.status-failed {
    background-color: var(--color-error);
    color: white;
}

.status-error {
    background-color: var(--color-error);
    color: white;
}

.download-button, .delete-button, .view-button {
    padding: 6px 12px;
    border-radius: 4px;
    font-size: 12px;
    text-decoration: none;
    color: white;
    display: inline-flex;
    align-items: center;
    cursor: pointer;
    border: none;
    margin-right: 4px;
}

.download-button {
    background-color: var(--color-success);
}

.download-button:hover {
    background-color: #388e3c;
}

.view-button {
    background-color: #2c7be5;
}

.view-button:hover {
    background-color: #1a68d1;
}

.delete-button {
    background-color: var(--color-error);
}

.delete-button:hover {
    background-color: #c62828;
}

.json-button {
    padding: 6px 12px;
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
}

.json-button:hover {
    background-color: #6d28d9;
}

.error-message {
    display: none;
    background-color: #ffebee;
    border: 1px solid var(--color-error);
    color: var(--color-error);
    padding: 12px 16px;
    border-radius: 4px;
    margin-bottom: 20px;
    font-size: 14px;
}

.error-message:not(:empty) {
    display: block;
}

.expandable-section summary {
    cursor: pointer;
    padding: 12px 16px;
    background-color: var(--color-background);
    border: 1px solid var(--color-border);
    border-radius: 4px;
    color: var(--color-text);
    list-style: none;
}

.expandable-section summary::-webkit-details-marker {
    display: none;
}

.expandable-section[open] summary {
    border-bottom: none;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
}

.expandable-section[open] fieldset {
    border: 1px solid var(--color-border);
    border-top: none;
    border-radius: 0 0 4px 4px;
    padding: 16px;
}

@media (max-width: 768px) {
    main {
        flex-direction: column;
    }

    .sidebar {
        width: 100%;
        border-right: none;
        border-bottom: 1px solid var(--color-border);
        box-shadow: 0 2px 5px rgba(0, 0, 0, 0.05);
    }

    .content {
        padding: 20px;
    }
}

.sponsor-section {
    position: relative;
    margin-top: 24px;
    padding: 16px;
    background-color: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 4px;
}

.sponsor-close {
    position: absolute;
    top: 8px;
    right: 8px;
    width: 20px;
    height: 20px;
    border: none;
    background-color: transparent;
    color: var(--color-text-light);
    font-size: 16px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: color 0.2s;
    padding: 0;
}

.sponsor-close:hover {
    background-color: transparent;
    color: var(--color-text);
}

.sponsor-text {
    margin: 0 0 12px 0;
    font-size: 14px;
    color: var(--color-text);
    padding-right: 20px;
}

.sponsor-button {
    display: inline-flex;
    align-items: center;
    background-color: var(--color-sponsor);
    color: white;
    border: none;
    padding: 10px 16px;
    border-radius: 4px;
    font-size: 14px;
    text-decoration: none;
    transition: background-color 0.2s;
}

.sponsor-button:hover {
    background-color: var(--color-sponsor-hover);
}

.sponsor-button svg {
    margin-right: 8px;
}

nav {
    margin-bottom: 16px;
    padding: 8px 0;
}

nav a {
    color: white;
    text-decoration: none;
    font-size: 16px;
    font-weight: 500;
    padding: 8px 16px;
    background-color: var(--color-primary);
    border: 1px solid var(--color-primary);
    border-radius: 4px;
    transition: all 0.2s ease;
    display: inline-block;
}

nav a:hover {
    background-color: var(--color-primary-light);
    transform: translateY(-1px);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
}

/* Spinner */
.spinner {
    display: none;
    width: 24px;
    height: 24px;
    border: 3px solid #ccc;
    border-top-color: var(--color-primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 12px;
}

.spinner.active {
    display: inline-block;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}

/* Map modal */
.map-modal {
    display: flex;
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: rgba(0, 0, 0, 0.6);
    justify-content: center;
    align-items: center;
}

.map-modal-content {
    background: #fff;
    width: 90vw;
    height: 85vh;
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.map-modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid #e0e0e0;
}

.map-modal-header h2 {
    margin: 0;
    font-size: 1.1rem;
}

.map-modal-close {
    background: none;
    border: none;
    font-size: 1.6rem;
    line-height: 1;
    cursor: pointer;
    color: #555;
    padding: 0;
}

.map-modal-close:hover {
    background: none;
    color: #222;
}

.map-modal-body {
    flex: 1;
    display: flex;
    min-height: 0;
}

#job-map {
    flex: 3;
    height: 100%;
}

#job-places-list {
    flex: 1;
    min-width: 240px;
    max-width: 360px;
    overflow-y: auto;
    border-left: 1px solid #e0e0e0;
    padding: 8px;
}

.place-item {
    padding: 8px;
    border-bottom: 1px solid #f0f0f0;
    cursor: pointer;
}

.place-item:hover {
    background: #f5f8ff;
}

.place-title {
    font-weight: 600;
}

.place-address {
    font-size: 0.85rem;
    color: #666;
}

.place-rating {
    font-size: 0.85rem;
    color: #d98300;
}

.map-loading, .map-empty, .map-error {
    padding: 12px;
    color: #666;
}

.map-error { color: #c0392b; }

.form-group.checkbox {
    display: flex;
    align-items: center;
    gap: 8px;
}

.form-group.checkbox label {
    margin-bottom: 0;
}

.form-group.checkbox input[type="checkbox"] {
    width: 16px;
    height: 16px;
}

.badge-cli {
    font-size: 10px;
    background: #fef3c7;
    color: #b45309;
    padding: 1px 5px;
    border-radius: 3px;
    font-weight: 700;
    margin-left: 4px;
}

.text-muted {
    font-size: 12px;
    color: var(--color-text-light);
}

.actions-cell {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
}
    </style>
</head>
<body>
    <div class="app-container">
        <header>
            <h1>Google Maps Scraper</h1>
            <nav>
                <a href="https://github.com/gosom/google-maps-scraper/blob/master/apidocs/swagger.json" target="_blank" rel="noopener noreferrer">API Documentation</a>
            </nav>
            <div class="github-section">
                <p>If you find this tool useful, please consider starring our repository: </p>
                <a href="https://github.com/gosom/google-maps-scraper" class="github-button" target="_blank" rel="noopener noreferrer">
                <svg height="16" width="16" viewBox="0 0 16 16">
                    <path fill="currentColor" d="M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.75.75 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25z"/>
                  </svg>
                  Star on GitHub
                </a>
            </div>
            <div id="sponsor-section" class="sponsor-section">
    <button class="sponsor-close" onclick="hideSponsor()">
        ×
    </button>
    <p class="sponsor-text">Support the ongoing maintenance and development of this project by becoming a sponsor. Your contribution helps ensure the tool's continued improvement and reliability.</p>
    <a href="https://github.com/sponsors/gosom" class="sponsor-button" target="_blank" rel="noopener noreferrer">
        <svg height="16" width="16" viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path fill-rule="evenodd" clip-rule="evenodd" d="M4.25 2.5C4.25 1.83696 4.67143 1.25 5.25 1.25C5.82857 1.25 6.25 1.83696 6.25 2.5C6.25 3.16304 5.82857 3.75 5.25 3.75C4.67143 3.75 4.25 3.16304 4.25 2.5ZM2.5 3.75C1.92143 3.75 1.5 3.16304 1.5 2.5C1.5 1.83696 1.92143 1.25 2.5 1.25C3.07857 1.25 3.5 1.83696 3.5 2.5C3.5 3.16304 3.07857 3.75 2.5 3.75ZM8 1.25C7.42143 1.25 7 1.83696 7 2.5C7 3.16304 7.42143 3.75 8 3.75C8.57857 3.75 9 3.16304 9 2.5C9 1.83696 8.57857 1.25 8 1.25ZM10.75 2.5C10.75 1.83696 11.1714 1.25 11.75 1.25C12.3286 1.25 12.75 1.83696 12.75 2.5C12.75 3.16304 12.3286 3.75 11.75 3.75C11.1714 3.75 10.75 3.16304 10.75 2.5ZM14.5 1.25C13.9214 1.25 13.5 1.83696 13.5 2.5C13.5 3.16304 13.9214 3.75 14.5 3.75C15.0786 3.75 15.5 3.16304 15.5 2.5C15.5 1.83696 15.0786 1.25 14.5 1.25ZM1.25 5.25C1.25 4.67143 1.83696 4.25 2.5 4.25C3.16304 4.25 3.75 4.67143 3.75 5.25C3.75 5.82857 3.16304 6.25 2.5 6.25C1.83696 6.25 1.25 5.82857 1.25 5.25ZM5.25 4.25C4.67143 4.25 4.25 4.67143 4.25 5.25C4.25 5.82857 4.67143 6.25 5.25 6.25C5.82857 6.25 6.25 5.82857 6.25 5.25C6.25 4.67143 5.82857 4.25 5.25 4.25ZM7 5.25C7 4.67143 7.42143 4.25 8 4.25C8.57857 4.25 9 4.67143 9 5.25C9 5.82857 8.57857 6.25 8 6.25C7.42143 6.25 7 5.82857 7 5.25ZM11.75 4.25C11.1714 4.25 10.75 4.67143 10.75 5.25C10.75 5.82857 11.1714 6.25 11.75 6.25C12.3286 6.25 12.75 5.82857 12.75 5.25C12.75 4.67143 12.3286 4.25 11.75 4.25ZM13.5 5.25C13.5 4.67143 13.9214 4.25 14.5 4.25C15.0786 4.25 15.5 4.67143 15.5 5.25C15.5 5.82857 15.0786 6.25 14.5 6.25C13.9214 6.25 13.5 5.82857 13.5 5.25ZM2.5 7.25C1.83696 7.25 1.25 7.67143 1.25 8.25C1.25 8.82857 1.83696 9.25 2.5 9.25C3.16304 9.25 3.75 8.82857 3.75 8.25C3.75 7.67143 3.16304 7.25 2.5 7.25ZM4.25 8.25C4.25 7.67143 4.67143 7.25 5.25 7.25C5.82857 7.25 6.25 7.67143 6.25 8.25C6.25 8.82857 5.82857 9.25 5.25 9.25C4.67143 9.25 4.25 8.82857 4.25 8.25ZM8 7.25C7.42143 7.25 7 7.67143 7 8.25C7 8.82857 7.42143 9.25 8 9.25C8.57857 9.25 9 8.82857 9 8.25C9 7.67143 8.57857 7.25 8 7.25ZM10.75 8.25C10.75 7.67143 11.1714 7.25 11.75 7.25C12.3286 7.25 12.75 7.67143 12.75 8.25C12.75 8.82857 12.3286 9.25 11.75 9.25C11.1714 9.25 10.75 8.82857 10.75 8.25ZM14.5 7.25C13.9214 7.25 13.5 7.67143 13.5 8.25C13.5 8.82857 13.9214 9.25 14.5 9.25C15.0786 9.25 15.5 8.82857 15.5 8.25C15.5 7.67143 15.0786 7.25 14.5 7.25Z" fill="currentColor"/>
        </svg>
        Sponsor on GitHub
    </a>
</div>

        </header>
        <main>
            <div class="sidebar">
                <div id="error-container" class="error-message"></div>
                <form id="scrape-form" onsubmit="handleSubmit(event)">

                    <fieldset>
                        <legend>Job Details</legend>
                        <div class="form-group">
                            <label for="name">Job Name:</label>
                            <input type="text" id="name" name="name" value="" autofocus>
                        </div>
                        <div class="form-group">
                            <label for="keywords">Keywords:</label>
                            <textarea id="keywords" name="keywords" rows="10"></textarea>
                        </div>
                        <div class="form-group">
                            <label for="lang">Language:</label>
                            <input type="text" id="lang" name="lang" value="en">
                        </div>
                    </fieldset>

                    <details class="expandable-section">
                        <summary>Location Settings</summary>
                        <fieldset>
                            <div class="form-group">
                                <label for="zoom">Zoom:</label>
                                <input type="number" id="zoom" name="zoom" value="15">
                            </div>
                            <div class="form-group">
                                <label for="latitude">Latitude:</label>
                                <input type="number" step="0.000000000000001" id="latitude" name="latitude" value="0">
                            </div>
                            <div class="form-group">
                                <label for="longitude">Longitude:</label>
                                <input type="number" step="0.000000000000001" id="longitude" name="longitude" value="0">
                            </div>
                        </fieldset>
                    </details>

                    <details class="expandable-section">
                        <summary>Advanced Options</summary>
                        <fieldset>
                            <div class="form-group checkbox">
                                <input type="checkbox" id="fastmode" name="fastmode">
                                <label for="fastmode">Fast Mode (BETA)</label>
                            </div>
                            <div class="form-group">
                                <label for="radius">Radius (BETA):</label>
                                <input type="number" id="radius" name="radius" value="10000">
                            </div>
                            <div class="form-group">
                                <label for="depth">Depth:</label>
                                <input type="number" step="1" id="depth" name="depth" value="10">
                            </div>
                            <div class="form-group checkbox">
                                <input type="checkbox" id="email" name="email">
                                <label for="email">Fetch Emails</label>
                            </div>
                            <div class="form-group">
                                <label for="maxtime">Max job time:</label>
                                <input type="text" id="maxtime" name="maxtime" value="10m">
                            </div>
                            <div class="form-group checkbox">
                                <input type="checkbox" id="extra_reviews" name="extra_reviews">
                                <label for="extra_reviews">Extra Reviews <span class="badge-cli">CLI</span></label>
                            </div>
                        </fieldset>
                    </details>

                    <details class="expandable-section">
                        <summary>Proxies</summary>
                        <fieldset>
                            <div class="form-group">
                                <label for="proxies">Proxies: (one per line)</label>
                                <p class="text-muted"><small>Examples:<br>
                                        HTTPS proxy with username/password: https://username:password@myproxy.local:443<br>
                                        HTTP proxy with username/password: http://username:password@myproxy.local:443<br>
                                        SOCKS5 proxy without auth: socks5://127.0.0.1:8000
                                </small></p>
                                <textarea id="proxies" name="proxies" rows="5"></textarea>
                            </div>
                        </fieldset>
                    </details>

                    <button type="submit" id="submit-btn">Start Scraping</button>
                </form>
            </div>
            <div class="content">
                <div id="spinner" class="spinner"></div>
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
                        <tr><td colspan="5" style="color:#999; padding:16px;">Loading jobs...</td></tr>
                    </tbody>
                </table>
                <div id="pagination-container"></div>
            </div>
        </main>
    </div>

    <!-- Map Modal -->
    <div id="map-modal-container"></div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
    <script>
    // ---- Sponsor hide ----
    function hideSponsor() {
        const el = document.getElementById('sponsor-section');
        if (el) el.style.display = 'none';
    }

    // ---- Error display ----
    function showError(msg) {
        const el = document.getElementById('error-container');
        el.textContent = msg;
        el.style.display = msg ? 'block' : 'none';
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
        const maxtimeStr = document.getElementById('maxtime').value.trim() || '10m';
        const maxtime = parseMaxTime(maxtimeStr);
        const extra_reviews = document.getElementById('extra_reviews').checked;
        const rawProxies = document.getElementById('proxies').value.trim();

        if (!name) { showError('Job Name is required.'); btn.disabled = false; btn.textContent = 'Start Scraping'; spinner.classList.remove('active'); return; }
        const keywords = rawKeywords.split('\\n').map(s => s.trim()).filter(s => s.length > 0);
        if (keywords.length === 0) { showError('At least one keyword is required.'); btn.disabled = false; btn.textContent = 'Start Scraping'; spinner.classList.remove('active'); return; }

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
            proxies
        };

        try {
            const res = await fetch('/api/jobs', {
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
                fetchJobs();
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
    function escapeHtml(str) {
        return String(str == null ? '' : str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    async function fetchJobs() {
        try {
            const res = await fetch('/api/jobs');
            if (!res.ok) return;
            const jobs = await res.json();
            renderJobs(jobs);
        } catch (err) {
            console.error('fetchJobs error:', err);
        }
    }

    function renderJobs(jobs) {
        const tbody = document.getElementById('job-tbody');
        if (!jobs || jobs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="color:#999; padding:16px;">No jobs yet. Submit the form to start scraping.</td></tr>';
            return;
        }

        let html = '';
        for (const j of jobs) {
            const id = escapeHtml(j.ID || '');
            const name = escapeHtml(j.Name || '—');
            const date = j.Date ? new Date(j.Date).toLocaleString() : '—';
            const status = j.Status || 'pending';

            let statusHtml = '';
            if (status === 'ok') {
                statusHtml = '<span class="status-indicator status-ok">ok</span>';
            } else if (status === 'working') {
                statusHtml = '<span class="status-indicator status-working">working</span>';
            } else if (status === 'pending') {
                statusHtml = '<span class="status-indicator status-pending">pending</span>';
            } else if (status === 'failed' || status === 'error') {
                statusHtml = '<span class="status-indicator status-failed">' + escapeHtml(status) + '</span>';
            } else {
                statusHtml = '<span class="status-indicator">' + escapeHtml(status) + '</span>';
            }

            let actionsHtml = '<div class="actions-cell">';
            if (status === 'ok') {
                actionsHtml += `<button class="view-button" onclick="openMapModal('${j.ID}', '${name}')">View</button>`;
                actionsHtml += `<a href="/download/${j.ID}?format=csv" class="download-button">CSV</a>`;
                actionsHtml += `<a href="/download/${j.ID}?format=json" class="json-button">JSON</a>`;
            }
            actionsHtml += `<button class="delete-button" onclick="deleteJob('${j.ID}')">Delete</button>`;
            actionsHtml += '</div>';

            const shortId = id.substring(0, 8);

            html += `<tr>
                <td title="${id}">${shortId}…</td>
                <td>${name}</td>
                <td>${date}</td>
                <td>${statusHtml}</td>
                <td>${actionsHtml}</td>
            </tr>`;
        }
        tbody.innerHTML = html;
    }

    async function deleteJob(id) {
        if (!confirm('Delete this job?')) return;
        try {
            await fetch('/api/jobs/' + id, { method: 'DELETE' });
            fetchJobs();
        } catch (err) {
            alert('Error deleting job: ' + err.message);
        }
    }

    // ---- Map Modal ----
    let leafletMap = null;
    let markersGroup = null;

    async function openMapModal(jobId, jobName) {
        // Build modal HTML
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

        // Init Leaflet
        if (leafletMap) {
            leafletMap.remove();
            leafletMap = null;
            markersGroup = null;
        }
        leafletMap = L.map('job-map');
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '© OpenStreetMap contributors'
        }).addTo(leafletMap);
        markersGroup = L.featureGroup().addTo(leafletMap);

        const listEl = document.getElementById('job-places-list');

        try {
            const res = await fetch('/api/preview/' + jobId);
            const data = await res.json();
            const places = data.sample || [];

            listEl.innerHTML = '';
            if (places.length === 0) {
                listEl.innerHTML = '<div class="map-empty">No coordinates found for this job.</div>';
                leafletMap.setView([20, 0], 2);
                return;
            }

            places.forEach((place) => {
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
                    ${link ? `<div><a href="${encodeURI(link)}" target="_blank" style="font-size:12px;color:#2c7be5;">Open on Google Maps ↗</a></div>` : ''}
                `;

                if (!isNaN(lat) && !isNaN(lon) && lat !== 0 && lon !== 0) {
                    const marker = L.marker([lat, lon]).addTo(markersGroup);
                    marker.bindPopup(`<strong>${escapeHtml(title)}</strong><br>${escapeHtml(address)}<br>${escapeHtml(phone)}`);
                    item.addEventListener('click', () => {
                        document.querySelectorAll('.place-item').forEach(el => el.style.background = '');
                        item.style.background = '#e8f0fe';
                        leafletMap.setView([lat, lon], 17);
                        marker.openPopup();
                    });
                }

                listEl.appendChild(item);
            });

            if (markersGroup.getLayers().length > 0) {
                leafletMap.fitBounds(markersGroup.getBounds(), { padding: [30, 30] });
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
            markersGroup = null;
        }
    }

    // ---- Init ----
    fetchJobs();
    setInterval(fetchJobs, 3000);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    try:
        req = urllib.request.Request(f"{BACKEND_URL}/api/v1/jobs")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return jsonify(data)
    except Exception as e:
        return jsonify([]), 200

@app.route("/api/jobs", methods=["POST"])
def create_job():
    try:
        payload = request.json
        req_data = json.dumps(payload).encode("utf-8")
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

@app.route("/download/<job_id>")
def download_job(job_id):
    fmt = request.args.get("format", "csv").lower()
    try:
        req = urllib.request.Request(f"{BACKEND_URL}/download?id={job_id}")
        with urllib.request.urlopen(req) as resp:
            content = resp.read()

        if fmt == "json":
            text = content.decode("utf-8", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            items = list(reader)
            json_data = json.dumps(items, indent=2, ensure_ascii=False)
            return Response(
                json_data,
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

@app.route("/api/preview/<job_id>")
def preview_job(job_id):
    try:
        req = urllib.request.Request(f"{BACKEND_URL}/download?id={job_id}")
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        return jsonify({"total": len(rows), "sample": rows})
    except Exception as e:
        return jsonify({"error": str(e), "sample": []}), 500

@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    try:
        req = urllib.request.Request(f"{BACKEND_URL}/delete?id={job_id}", method="DELETE")
        with urllib.request.urlopen(req) as resp:
            return jsonify({"status": "deleted"}), 200
    except Exception as e:
        return Response(str(e), status=500)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
