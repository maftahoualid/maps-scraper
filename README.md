# Google Maps Scraper UI (Gosom Web Wrapper)

A production-ready Web UI and proxy wrapper for [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper).

Restores the clean, original gosom visual interface while fixing form submission issues (bypassing htmx CDN blocks via native asynchronous API calls) and adding extra features:
- **Original Gosom Design**: Clean sidebar layout, responsive table, and status badges.
- **Reliable Form Submission**: Native asynchronous REST client replacing htmx dependencies.
- **Interactive Map View**: Integrated Leaflet map to inspect scraped locations directly in the browser.
- **JSON & CSV Export**: Download results in both CSV and formatted JSON.
- **CLI Flags Support**: Support for `extra_reviews`, `email` extraction, `fast_mode`, custom coordinates, and proxies.

---

## 🚀 Quick Start (Docker Compose)

The easiest way to run the entire stack is with Docker Compose:

```bash
docker compose up -d
```

Open your browser at **`http://localhost:8080`**.

To stop:
```bash
docker compose down
```

---

## 🛠 Manual Setup / Docker Run

### 1. Run the Backend (Gosom Engine)
```bash
docker run -d --name gmaps-backend \
  -p 127.0.0.1:8081:8080 \
  -v "$HOME/gmaps-webdata:/gmapsdata" \
  --restart unless-stopped \
  gosom/google-maps-scraper -web -data-folder /gmapsdata
```

### 2. Build and Run the UI
```bash
docker build -t gmaps-ui .
docker run -d --name gmaps-ui \
  --network host \
  --restart unless-stopped \
  -e BACKEND_URL=http://127.0.0.1:8081 \
  gmaps-ui:latest
```

---

## 🖥 Local CLI Utility (`gmaps`)

An optional CLI helper script is available in `bin/gmaps`:

```bash
# Install to your local bin
cp bin/gmaps ~/.local/bin/gmaps
chmod +x ~/.local/bin/gmaps

# Usage
gmaps start    # Starts both backend and frontend containers
gmaps status   # Checks running status
gmaps logs     # Streams live backend scraper logs
gmaps stop     # Stops the services
```

---

## 📁 Repository Structure

```text
├── app.py             # Flask application & embedded frontend UI
├── Dockerfile         # Docker image definition for the UI
├── docker-compose.yml # Compose file to orchestrate backend + UI
├── bin/
│   └── gmaps          # CLI management script
└── README.md
```

## License
MIT
