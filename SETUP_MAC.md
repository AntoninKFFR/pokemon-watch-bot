# Setup Mac

## Installation
```bash
cd pokemon-watch-bot
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Google Sheets
1. Place `google_service_account.json` dans le dossier du projet
2. Crée un Google Sheet nommé `Pokemon Deals Watch`
3. Partage le Google Sheet avec l'email du service account en **éditeur**

## Lancer le bot
```bash
python3 main.py
```

## Scripts pratiques
```bash
./start_auto.sh
./stop_auto.sh
./status_auto.sh
```
