@echo off
chcp 65001 > nul
title GNL Edge Monitor

echo.
echo  ╔══════════════════════════════════════════════════════════╗
echo  ║         GNL EDGE MONITOR — WINDOWS STANDALONE            ║
echo  ║         Systeme IoT Distribue  M2 RSID                   ║
echo  ╠══════════════════════════════════════════════════════════╣
echo  ║  Dashboard  : http://localhost:5000                       ║
echo  ║  Login      : admin / admin_GNL_2025!                    ║
echo  ║  Historique : SQLite local (gnl_history.db)              ║
echo  ║  IA         : Gemma4 localhost:8080 (si Docker tourne)   ║
echo  ╚══════════════════════════════════════════════════════════╝
echo.

REM ── Liberer le port 5000 si deja occupe ──────────────────────────────────────
echo  Verification port 5000...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":5000 "') do (
    echo  Port 5000 occupe par PID %%a — fermeture...
    taskkill /PID %%a /F >nul 2>&1
)

REM ── Verifier Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERREUR: Python non trouve. Installez Python 3.10+ depuis python.org
    pause
    exit /b 1
)

REM ── Installer les dependances manquantes ─────────────────────────────────────
echo  Verification des dependances Python...
python -c "import flask, jwt, sklearn, numpy, requests, pymongo" >nul 2>&1
if errorlevel 1 (
    echo  Installation des dependances...
    pip install flask flask-cors pyjwt scikit-learn numpy requests pymongo --quiet
    if errorlevel 1 (
        echo  ERREUR installation. Essai avec --user...
        pip install flask flask-cors pyjwt scikit-learn numpy requests pymongo --user --quiet
    )
)

REM ── Gemma4 Docker disponible ? ───────────────────────────────────────────────
echo  Verification Gemma4 (localhost:8080)...
curl -sf http://localhost:8080/health >nul 2>&1
if not errorlevel 1 (
    echo  Gemma4 detecte sur localhost:8080 — IA LLM activee
    set GEMMA4_HOST=localhost
    set GEMMA4_SERVER_PORT=8080
    set GEMMA4_TIMEOUT=45
) else (
    echo  Gemma4 absent — IA regle-fixe activee (fallback)
)

REM ── Lancement ────────────────────────────────────────────────────────────────
echo.
echo  Demarrage du systeme...
echo  (Ctrl+C pour arreter)
echo.

REM Mode avec Arduino reel: LANCER_WINDOWS.bat --port COM3
REM Mode simulation       : LANCER_WINDOWS.bat (sans argument)

python start_gnl_windows.py %*

echo.
echo  Systeme arrete.
pause
