# pokemon-watch-bot

Bot de veille Pokemon Japon orienté Yahoo Auctions Japan et ZenMarket, en mode **watch-only**.

## Sécurité
- Le bot **n'achète jamais automatiquement**.
- L'achat se fait manuellement via ZenMarket.
- Ne jamais commit sur GitHub : `google_service_account.json`, `deals.db`, `pokemon_deals_export.csv`, `bot.log`, `.env`, `.venv`.
- Ne jamais publier de token Telegram, clé Google ou autre credential.

## Ce que fait le bot
- Cherche des annonces publiques Yahoo Auctions Japan
- Détecte les annonces intéressantes
- Enregistre les deals dans SQLite
- Exporte un CSV local
- Synchronise plusieurs onglets Google Sheets lisibles en français
- Permet de compléter manuellement les prix de revente depuis `Needs Price`
- Recalcule automatiquement le ROI, la marge et le statut au run suivant

## Fichiers principaux
- `main.py` : exécution du bot
- `config.py` : configuration
- `requirements.txt` : dépendances Python
- `market_prices.csv` : base de prix manuelle
- `product_aliases.csv` : alias produits / requêtes de recherche
- `keywords_*.txt` : mots-clés de recherche
- `start_auto.sh`, `stop_auto.sh`, `status_auto.sh` : automatisation macOS/Linux
- `install_windows.bat`, `run_bot.bat`, `start_auto_windows.ps1`, `stop_auto_windows.ps1`, `status_auto_windows.ps1` : installation et automatisation Windows

## Google Sheets
Le bot peut synchroniser ces onglets :
- `Deals` : vue complète
- `Best Deals` : meilleures opportunités lisibles
- `Buy Now Deals` : focus achat immédiat / prix fixe
- `Auctions Watch` : focus enchères à surveiller
- `Needs Price` : feuille de saisie manuelle des prix de revente
- `Mode d’emploi` : rappel d'utilisation

## Comment remplir `Needs Price`
1. Aller dans l'onglet `Needs Price`
2. Remplir uniquement les colonnes jaunes :
   - `Prix revente manuel €`
   - `Source prix manuel`
   - `Fiabilité prix manuel`
   - `Statut manuel`
   - `Notes`
3. Mettre `Statut manuel = Validé` quand le prix est confirmé
4. Relancer :

```bash
python3 main.py
```

Le bot recalcule alors :
- `Prix marché €`
- `Marge estimée €`
- `ROI %`
- `Statut`
- `Action requise`

## Installation rapide
### Mac
Voir `SETUP_MAC.md`

### Windows
Voir `SETUP_WINDOWS.md`

## Lancer le bot
### Mac / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 main.py
```

### Windows PowerShell
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

## Variables d'environnement utiles
Un exemple est fourni dans `.env.example`.

Variables principales :
- `GOOGLE_SHEETS_ENABLED`
- `GOOGLE_SHEET_NAME`
- `GOOGLE_WORKSHEET_NAME`
- `GOOGLE_SERVICE_ACCOUNT_FILE`
- `TELEGRAM_ENABLED`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Automatisation
### macOS / Linux
Activer :
```bash
./start_auto.sh
```

Vérifier :
```bash
./status_auto.sh
```

Désactiver :
```bash
./stop_auto.sh
```

### Windows
Activer :
```powershell
powershell -ExecutionPolicy Bypass -File .\start_auto_windows.ps1
```

Vérifier :
```powershell
powershell -ExecutionPolicy Bypass -File .\status_auto_windows.ps1
```

Désactiver :
```powershell
powershell -ExecutionPolicy Bypass -File .\stop_auto_windows.ps1
```

## Mac / Windows : différence pratique
- macOS utilise `python3`, `bash`, `cron`
- Windows utilise `python`, `PowerShell`, `schtasks`
- La logique du bot reste la même sur les deux plateformes

## Avertissement GitHub
Avant un `git add .`, vérifie que tu ne publies jamais :
- `google_service_account.json`
- `.env`
- `deals.db`
- `pokemon_deals_export.csv`
- `bot.log`

Le `.gitignore` du projet est prévu pour ça.
