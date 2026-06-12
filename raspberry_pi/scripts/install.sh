#!/usr/bin/env bash
# install.sh — Installation complète du système GNL sur Raspberry Pi 4
#
# Prérequis : Raspberry Pi OS Bullseye/Bookworm, Python 3.11+
# Durée estimée : 10-15 min (selon connexion internet)
# Exécuter : sudo bash install.sh

set -euo pipefail
LOG="/var/log/gnl_install.log"
mkdir -p /var/log/gnl
exec > >(tee -a "$LOG") 2>&1

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

info "=== Installation GNL Edge Node — Raspberry Pi 4 ==="
info "Log : $LOG"

# ── 1. Mise à jour système ─────────────────────────────────────────────────────
info "1/7 — Mise à jour du système..."
apt-get update -qq && apt-get upgrade -y -qq

# ── 2. Dépendances système ─────────────────────────────────────────────────────
info "2/7 — Installation des dépendances système..."
apt-get install -y -qq \
    python3-pip python3-venv python3-serial \
    mosquitto mosquitto-clients \
    influxdb2 influxdb2-cli \
    git curl openssl ufw fail2ban \
    libopenblas-dev libatlas-base-dev

# ── 3. Dossier projet ─────────────────────────────────────────────────────────
info "3/7 — Création structure projet..."
INSTALL_DIR="/opt/gnl"
mkdir -p "$INSTALL_DIR"
mkdir -p /var/log/gnl
chown -R pi:pi /var/log/gnl

# Copie des fichiers (si exécuté depuis le dossier du projet)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp -r "$SCRIPT_DIR"/../raspberry_pi/* "$INSTALL_DIR/" 2>/dev/null || true
chown -R pi:pi "$INSTALL_DIR"

# ── 4. Environnement Python ───────────────────────────────────────────────────
info "4/7 — Installation des packages Python..."
pip3 install --break-system-packages \
    pyserial \
    paho-mqtt \
    influxdb-client \
    scikit-learn \
    numpy \
    pandas \
    flask \
    PyJWT \
    requests

info "    ✓ Packages Python installés"

# ── 5. InfluxDB — setup initial ───────────────────────────────────────────────
info "5/7 — Configuration InfluxDB..."
systemctl enable --now influxdb

sleep 3  # attendre le démarrage

# Setup organisation et bucket via CLI
influx setup \
    --username gnl_admin \
    --password "GNL_Influx_2025!" \
    --org gnl_org \
    --bucket gnl_monitoring \
    --retention 30d \
    --force 2>/dev/null || warn "InfluxDB déjà configuré (normal)"

# Politique de rétention 30 jours déjà définie ci-dessus

info "    ✓ InfluxDB configuré (rétention 30 jours)"

# ── 6. Service systemd ────────────────────────────────────────────────────────
info "6/7 — Installation service systemd..."
cp "$SCRIPT_DIR"/../raspberry_pi/systemd/gnl.service /etc/systemd/system/gnl.service
systemctl daemon-reload
systemctl enable gnl.service
info "    ✓ Service gnl.service installé (démarrage automatique)"

# ── 7. Sécurité ───────────────────────────────────────────────────────────────
info "7/7 — Configuration sécurité..."
bash "$SCRIPT_DIR"/../raspberry_pi/security/setup_security.sh
info "    ✓ Sécurité configurée"

# ── Résumé final ──────────────────────────────────────────────────────────────
info ""
info "╔══════════════════════════════════════════════════╗"
info "║     ✅  Installation GNL terminée avec succès    ║"
info "╚══════════════════════════════════════════════════╝"
info ""
info "Démarrage du système :"
info "  sudo systemctl start gnl"
info "  sudo systemctl status gnl"
info ""
info "Accès :"
info "  Dashboard : http://$(hostname -I | awk '{print $1}'):5000"
info "  Grafana   : http://$(hostname -I | awk '{print $1}'):3000"
info "  Logs      : journalctl -fu gnl"
info ""
warn "Prochaine étape : brancher l'Arduino et lancer 'systemctl start gnl'"
