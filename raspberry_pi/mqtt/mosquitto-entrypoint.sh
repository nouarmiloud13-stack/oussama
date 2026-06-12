#!/bin/sh
# ============================================================
# mosquitto-entrypoint.sh
# Crée /mosquitto/data/passwd au démarrage à partir des variables
# MQTT_USER et MQTT_PASSWORD (injectées par docker-compose).
#
# POURQUOI /data et pas /config ?
#   /mosquitto/config → bind-mount :ro (mosquitto.conf, acl)
#                        → lecture seule, écriture impossible
#   /mosquitto/data   → volume nommé Docker
#                        → inscriptible, persiste entre redémarrages
# ============================================================
set -e

PASSWD_FILE="/mosquitto/data/passwd"

# --- Vérification des variables obligatoires ---
if [ -z "$MQTT_USER" ]; then
  echo "[entrypoint] ERREUR : MQTT_USER non défini." >&2
  exit 1
fi
if [ -z "$MQTT_PASSWORD" ]; then
  echo "[entrypoint] ERREUR : MQTT_PASSWORD non défini." >&2
  exit 1
fi

# --- Créer /mosquitto/data s'il n'existe pas encore ---
mkdir -p /mosquitto/data /mosquitto/log

# --- Générer le fichier passwd (hash bcrypt via mosquitto_passwd) ---
# -c : crée/écrase le fichier
# -b : mode batch (pas interactif)
mosquitto_passwd -c -b "$PASSWD_FILE" "$MQTT_USER" "$MQTT_PASSWORD"

# --- Permissions strictes ---
chmod 600 "$PASSWD_FILE"
# Pas de chown sur /mosquitto/config (monté :ro) — uniquement sur le passwd
chown mosquitto:mosquitto "$PASSWD_FILE" 2>/dev/null || true

echo "[entrypoint] passwd créé : $PASSWD_FILE (user=$MQTT_USER)"

# --- Handoff à Mosquitto ---
exec mosquitto -c /mosquitto/config/mosquitto.conf "$@"
