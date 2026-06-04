# Setup Windows

## Prérequis
- Installer Python 3.11 ou plus récent depuis [python.org](https://www.python.org/downloads/windows/)
- Cocher l'option **Add Python to PATH** pendant l'installation
- Installer Git si besoin

## Installation
1. Ouvrir PowerShell
2. Cloner le dépôt
3. Aller dans le dossier du projet

```powershell
git clone <URL_DU_REPO>
cd pokemon-watch-bot
```

4. Créer l'environnement virtuel

```powershell
python -m venv .venv
```

5. Activer l'environnement virtuel

```powershell
.\.venv\Scripts\Activate.ps1
```

6. Installer les dépendances

```powershell
python -m pip install -r requirements.txt
```

## Google Sheets
1. Placer `google_service_account.json` dans le dossier du projet
2. Créer un Google Sheet nommé `Pokemon Deals Watch`
3. Partager ce Google Sheet avec l'adresse email du service account en **éditeur**
4. Si besoin, copier `.env.example` vers `.env` ou définir les variables d'environnement à la main

## Lancer le bot

```powershell
python main.py
```

## Script pratique
Tu peux aussi utiliser :

```powershell
.\install_windows.bat
.\run_bot.bat
```
