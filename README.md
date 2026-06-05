# AudioLogger

Professionele radio-audiologger webapplicatie die internetstreams automatisch opneemt, archiveert en afspeelbaar maakt via een web-dashboard.

## Stack

- Python 3.12 + FastAPI
- APScheduler (cron-based opnames)
- ffmpeg (opname & trim)
- SQLite + SQLModel
- Jinja2 + TailwindCSS
- Wavesurfer.js (audio editor)
- Docker + Railway

## Lokaal draaien

```bash
# Vereisten: Python 3.12, ffmpeg
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
```

Open http://localhost:8000

## Zenders configureren

Bewerk `config/stations.yaml` om zenders toe te voegen of te wijzigen. Herstart de app om wijzigingen door te voeren.

## Docker

```bash
docker build -t audiologger .
docker run -p 8000:8000 -v audiologger-data:/app/recordings audiologger
```

## Railway deployment

1. Push naar GitHub
2. Maak een nieuw project op [Railway](https://railway.app)
3. Koppel je GitHub repository
4. Voeg een persistent volume toe op `/app/recordings`
5. Deploy

De `railway.toml` en `Dockerfile` zijn al geconfigureerd.
