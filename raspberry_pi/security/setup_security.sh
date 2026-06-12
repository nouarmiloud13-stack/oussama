#!/usr/bin/env bash
# setup_security.sh — Configuration sécurité complète du système GNL
# Exécuter en root sur le Raspberry Pi 4
#
# Actions :
#   1. Génération des certificats TLS X.509 auto-signés (CA + serveur + client)
#   2. Configuration Mosquitto avec TLS + ACL + authentification
#   3. Configuration firewall UFW
#   4. Configuration fail2ban (protection SSH + MQTT)
#   5. Durcissement SSH
#
# Normes : IEC 62443-3-3, NIST SP 800-183

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="/var/log/gnl/setup_security.log"

# Couleurs
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*" | tee -a "$LOG"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*" | tee -a "$LOG"; }
error() { echo -e "${RED}[ERROR]${NC} $*" | tee -a "$LOG"; exit 1; }

[[ $EUID -ne 0 ]] && error "Ce script doit être exécuté en root (sudo)"

mkdir -p /var/log/gnl
info "=== Setup Sécurité GNL ==="

# ── 1. Dépendances ─────────────────────────────────────────────────────────────
info "Installation des dépendances..."
apt-get update -qq
apt-get install -y -qq mosquitto mosquitto-clients openssl ufw fail2ban

# ── 2. Certificats TLS X.509 ──────────────────────────────────────────────────
CERT_DIR="/etc/mosquitto/certs"
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

DAYS=825  # < 2 ans (limite Apple/Chrome)
KEY_SIZE=4096

info "Génération CA (Autorité de Certification)..."
openssl genrsa -out ca.key $KEY_SIZE 2>/dev/null
openssl req -new -x509 -days $DAYS -key ca.key -out ca.crt \
  -subj "/C=DZ/ST=Oran/L=Oran/O=GNL_M2_RSID/CN=GNL-CA" 2>/dev/null

info "Génération certificat serveur Mosquitto..."
openssl genrsa -out server.key $KEY_SIZE 2>/dev/null
openssl req -new -key server.key -out server.csr \
  -subj "/C=DZ/ST=Oran/L=Oran/O=GNL_M2_RSID/CN=localhost" 2>/dev/null
openssl x509 -req -days $DAYS -in server.csr \
  -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt 2>/dev/null

info "Génération certificat client (Raspberry Pi publisher)..."
openssl genrsa -out client.key $KEY_SIZE 2>/dev/null
openssl req -new -key client.key -out client.csr \
  -subj "/C=DZ/ST=Oran/L=Oran/O=GNL_M2_RSID/CN=gnl_rpi4" 2>/dev/null
openssl x509 -req -days $DAYS -in client.csr \
  -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out client.crt 2>/dev/null

# Permissions restrictives
chmod 600 *.key
chmod 644 *.crt
chown -R mosquitto:mosquitto "$CERT_DIR"
info "✓ Certificats TLS générés dans $CERT_DIR"

# ── 3. Mosquitto — configuration ───────────────────────────────────────────────
info "Configuration Mosquitto..."
cp "$SCRIPT_DIR/mosquitto.conf" /etc/mosquitto/mosquitto.conf
cp "$SCRIPT_DIR/acl" /etc/mosquitto/acl

# Création des utilisateurs MQTT
info "Création des utilisateurs MQTT..."
touch /etc/mosquitto/passwd

# Mots de passe (à changer en production)
mosquitto_passwd -b /etc/mosquitto/passwd gnl_publisher  "GNL_Secure_2025!"
mosquitto_passwd -b /etc/mosquitto/passwd gnl_dashboard  "GNL_Dash_2025!"
mosquitto_passwd -b /etc/mosquitto/passwd gnl_admin      "GNL_Admin_2025!"

chmod 640 /etc/mosquitto/passwd
chown root:mosquitto /etc/mosquitto/passwd
info "✓ Mosquitto configuré"

# ── 4. Firewall UFW ───────────────────────────────────────────────────────────
info "Configuration UFW (firewall)..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing

# SSH (limitée — 6 tentatives max / 30s)
ufw limit 22/tcp

# MQTT TLS uniquement
ufw allow 8883/tcp comment "MQTT TLS"

# Dashboard + API (réseau local uniquement)
ufw allow from 192.168.0.0/16 to any port 5000 comment "API REST (LAN)"
ufw allow from 192.168.0.0/16 to any port 3000 comment "Grafana (LAN)"
ufw allow from 192.168.0.0/16 to any port 8086 comment "InfluxDB (LAN)"

# Activation
ufw --force enable
ufw status verbose
info "✓ UFW configuré"

# ── 5. fail2ban ───────────────────────────────────────────────────────────────
info "Configuration fail2ban..."
cat > /etc/fail2ban/jail.d/gnl.conf << 'EOF'
[sshd]
enabled  = true
port     = 22
maxretry = 5
bantime  = 3600
findtime = 600

[mosquitto]
enabled  = true
port     = 8883
filter   = mosquitto
logpath  = /var/log/mosquitto/mosquitto.log
maxretry = 3
bantime  = 7200
findtime = 300
EOF

# Filtre Mosquitto
cat > /etc/fail2ban/filter.d/mosquitto.conf << 'EOF'
[Definition]
failregex = .*Client .* disconnected due to protocol error.*
            .*Socket error .* on client .*, disconnecting.*
            .*Client .*, bad username or password.*
ignoreregex =
EOF

systemctl restart fail2ban
info "✓ fail2ban configuré"

# ── 6. Durcissement SSH ────────────────────────────────────────────────────────
info "Durcissement SSH..."
# Sauvegarde config originale
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak

# Paramètres sécurisés
sed -i 's/#PermitRootLogin.*/PermitRootLogin no/'           /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/#MaxAuthTries.*/MaxAuthTries 3/'                   /etc/ssh/sshd_config
sed -i 's/#LoginGraceTime.*/LoginGraceTime 30/'              /etc/ssh/sshd_config

echo "AllowUsers pi" >> /etc/ssh/sshd_config

# Vérification avant reload
sshd -t && systemctl reload sshd || warn "Vérifier config SSH manuellement"
info "✓ SSH durci (root login désactivé, password auth désactivé)"

# ── 7. Services systemd ────────────────────────────────────────────────────────
info "Activation des services..."
systemctl enable --now mosquitto
info "✓ Mosquitto activé"

# ── Résumé ────────────────────────────────────────────────────────────────────
info ""
info "=== ✅ Sécurité GNL configurée ==="
info ""
info "Résumé :"
info "  TLS  : certificats dans $CERT_DIR"
info "  MQTT : port 8883 uniquement (TLS)"
info "  UFW  : SSH limité, API/Grafana LAN only"
info "  SSH  : root login + password auth désactivés"
info ""
warn "IMPORTANT : Changer les mots de passe MQTT avant déploiement !"
warn "  mosquitto_passwd -b /etc/mosquitto/passwd gnl_publisher NOUVEAU_MOT_DE_PASSE"
