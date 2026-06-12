#!/usr/bin/env python3
"""
mqtt_client.py — Client MQTT (sans TLS) pour le système GNL

BUG #3 CORRIGÉ :
  - clean_session=True supprimé du constructeur mqtt.Client().
    Dans paho-mqtt 2.0.0, passer clean_session avec protocol=MQTTv5
    lève immédiatement :
        ValueError: Clean session is not used for MQTT 5.0
    → le conteneur gnl_edge_node crashait dès l'instanciation de
      GNLMQTTClient(), avant même la première tentative de connexion.
  - La session est maintenant gérée via le paramètre clean_start de
    connect() : MQTT_CLEAN_START_FIRST_ONLY (défaut paho) → session
    propre uniquement à la première connexion, conservée sur reconnexion.

Broker : Mosquitto sur port 1883 (plain MQTT — prototype localhost)
Sécurité : authentification username/password uniquement
Topics : gnl/niveau, gnl/temperature, gnl/gaz, gnl/pression,
         gnl/ia, gnl/alerte, gnl/cmd

⚠️  TLS délibérément désactivé pour le développement local.
    En production : réactiver TLS (port 8883, tls_set avec
    cafile/certfile/keyfile, tls_insecure_set(False))
    conformément à IEC 62443-3-3.

Compatible : paho-mqtt 2.0.0 (CallbackAPIVersion.VERSION2 obligatoire)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

log = logging.getLogger("gnl.mqtt")

# ── Configuration — lecture des variables d'environnement ──────────────────────
BROKER_HOST = os.environ.get("MQTT_HOST", "localhost")
BROKER_PORT = int(os.environ.get("MQTT_PORT", "1883"))
KEEPALIVE   = 60
CLIENT_ID   = "gnl_rpi4_edge"

MQTT_USER = os.environ.get("MQTT_USER_PUBLISHER", "gnl_publisher")
MQTT_PASS = os.environ.get("MQTT_PASS_PUBLISHER", "GNL_Secure_2025!")

# Topics avec leur QoS par défaut
TOPICS = {
    "niveau_r1": ("gnl/niveau/r1",      1),
    "niveau_r2": ("gnl/niveau/r2",      1),
    "temp_r1":   ("gnl/temperature/r1", 1),
    "temp_r2":   ("gnl/temperature/r2", 1),
    "gaz":       ("gnl/gaz/mq4",        2),
    "pression":  ("gnl/pression",       0),
    "ia_score":  ("gnl/ia/score",       1),
    "alerte":    ("gnl/alerte",         2),
    "cmd_pompe": ("gnl/cmd/pompe",      2),
    "cmd_vanne": ("gnl/cmd/vanne",      2),
    "cmd_esd":   ("gnl/cmd/esd",        2),
}

RECONNECT_DELAY = 5  # secondes entre chaque tentative de reconnexion


class GNLMQTTClient:
    """Client MQTT avec reconnexion automatique pour le système IoT GNL.

    Utilise l'API paho-mqtt 2.0.0 (CallbackAPIVersion.VERSION2) :
    - on_connect    : (client, userdata, connect_flags, reason_code, properties)
    - on_disconnect : (client, userdata, disconnect_flags, reason_code, properties)
    - on_message    : (client, userdata, message)
    """

    def __init__(self) -> None:
        # ── BUG #3 CORRIGÉ ────────────────────────────────────────────────────
        # clean_session=True RETIRÉ.
        # Raison : paho-mqtt 2.0.0 lève ValueError si clean_session est passé
        # avec protocol=MQTTv5 (la spec MQTT 5.0 a remplacé "clean session"
        # par "clean start", géré dans connect() via clean_start=).
        # L'ancien code causait un crash immédiat du conteneur gnl_edge_node
        # dès la ligne GNLMQTTClient() dans gnl_main.py.
        # ─────────────────────────────────────────────────────────────────────
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
            protocol=mqtt.MQTTv5,
        )
        self._connected = False
        self._setup_client()
        log.info(
            "GNLMQTTClient initialisé — broker=%s:%d (plain MQTT, sans TLS)",
            BROKER_HOST,
            BROKER_PORT,
        )

    def _setup_client(self) -> None:
        # Authentification username/password
        self._client.username_pw_set(MQTT_USER, MQTT_PASS)

        # Callbacks (signatures VERSION2)
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

        # Last Will Testament — détecte la déconnexion inopinée du nœud Edge
        self._client.will_set(
            "gnl/status",
            json.dumps({"status": "offline", "node": CLIENT_ID}),
            qos=1,
            retain=True,
        )

    # ── Connexion / déconnexion ────────────────────────────────────────────────

    def connect(self) -> None:
        """Connexion bloquante avec retry jusqu'au succès."""
        while True:
            try:
                # clean_start=MQTT_CLEAN_START_FIRST_ONLY (défaut paho) :
                # session propre à la première connexion seulement.
                self._client.connect(BROKER_HOST, BROKER_PORT, KEEPALIVE)
                self._client.loop_start()
                log.info("MQTT connecté : %s:%d", BROKER_HOST, BROKER_PORT)
                # Laisser la boucle réseau s'établir avant de publier
                time.sleep(0.5)
                self._publish_raw(
                    "gnl/status",
                    json.dumps({"status": "online", "node": CLIENT_ID}),
                    qos=1,
                    retain=True,
                )
                return
            except Exception as exc:
                log.warning(
                    "MQTT connexion échouée (%s) — retry dans %ds",
                    exc,
                    RECONNECT_DELAY,
                )
                time.sleep(RECONNECT_DELAY)

    def disconnect(self) -> None:
        """Déconnexion propre avec publication du statut offline."""
        self._publish_raw(
            "gnl/status",
            json.dumps({"status": "offline", "node": CLIENT_ID}),
            qos=1,
            retain=True,
        )
        self._client.loop_stop()
        self._client.disconnect()
        log.info("MQTT déconnecté proprement")

    # ── Publication des mesures ────────────────────────────────────────────────

    def publish_all(self, data: dict) -> None:
        """Publie toutes les mesures depuis le dict Arduino enrichi par l'IA."""
        ts = datetime.now(timezone.utc).isoformat()
        ai = data.get("ai", {})

        payloads: dict[str, tuple[dict, int]] = {
            "niveau_r1": (
                {"valeur": data.get("n1"), "unite": "%", "timestamp": ts},
                TOPICS["niveau_r1"][1],
            ),
            "niveau_r2": (
                {"valeur": data.get("n2"), "unite": "%", "timestamp": ts},
                TOPICS["niveau_r2"][1],
            ),
            "temp_r1": (
                {"valeur": data.get("t1"), "unite": "°C", "timestamp": ts},
                TOPICS["temp_r1"][1],
            ),
            "temp_r2": (
                {"valeur": data.get("t2"), "unite": "°C", "timestamp": ts},
                TOPICS["temp_r2"][1],
            ),
            "gaz": (
                {
                    "valeur":    data.get("g"),
                    "unite":     "ADC",
                    "niveau":    self._gas_level(data.get("g", 0)),
                    "timestamp": ts,
                },
                TOPICS["gaz"][1],
            ),
            "pression": (
                {"valeur": data.get("p"), "unite": "hPa", "timestamp": ts},
                TOPICS["pression"][1],
            ),
            "ia_score": (
                {
                    "isolation_forest": ai.get("isolation_forest", 0),
                    "global_risk":      ai.get("global_risk", 0),
                    "gas_alert":        ai.get("gas_alert"),
                    "overflow_risk":    ai.get("regression", {}).get("overflow_risk", False),
                    "timestamp":        ts,
                },
                TOPICS["ia_score"][1],
            ),
        }

        for key, (payload, qos) in payloads.items():
            topic = TOPICS[key][0]
            self._publish(topic, payload, qos)

        # Alertes conditionnelles
        gas_alert = ai.get("gas_alert")
        if gas_alert and gas_alert != "ATTENTION":
            self._publish_alert(gas_alert, data.get("g", 0), ts)

        global_risk = ai.get("global_risk", 0)
        if global_risk >= 70:
            self._publish_alert(f"RISQUE_{global_risk}", global_risk, ts)

    # ── Helpers de publication ─────────────────────────────────────────────────

    def _publish(self, topic: str, payload: dict, qos: int = 1) -> None:
        """Sérialise en JSON et publie sur un topic MQTT."""
        if not self._connected:
            log.debug("MQTT non connecté — message ignoré : %s", topic)
            return
        try:
            self._client.publish(topic, json.dumps(payload), qos=qos)
        except Exception as exc:
            log.warning("Erreur publication %s : %s", topic, exc)

    def _publish_raw(
        self, topic: str, payload: str, qos: int = 1, retain: bool = False
    ) -> None:
        """Publie une chaîne brute (sans sérialisation JSON supplémentaire)."""
        try:
            self._client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as exc:
            log.warning("Erreur publication raw %s : %s", topic, exc)

    def _publish_alert(
        self, alert_type: str, value: object, timestamp: str
    ) -> None:
        severity = "CRITIQUE" if "DANGER" in str(alert_type) else "ÉLEVÉ"
        payload = {
            "type":      alert_type,
            "severity":  severity,
            "valeur":    value,
            "timestamp": timestamp,
            "node":      CLIENT_ID,
        }
        topic, qos = TOPICS["alerte"]
        self._publish(topic, payload, qos)
        log.warning("ALERTE publiée : %s (valeur=%s)", alert_type, value)

    @staticmethod
    def _gas_level(gas: int) -> str:
        if gas < 250:
            return "OK"
        if gas < 450:
            return "ATTENTION"
        return "DANGER"

    # ── Callbacks MQTT (signatures paho-mqtt 2.0.0 — VERSION2) ────────────────

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        connect_flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties,
    ) -> None:
        if reason_code.is_failure:
            log.error("MQTT connexion refusée : %s", reason_code)
            return

        self._connected = True
        log.info("MQTT connecté (reason_code=%s)", reason_code)

        # Souscription aux topics de commande (opérateur → nœud Edge)
        for cmd_key in ("cmd_pompe", "cmd_vanne", "cmd_esd"):
            topic, qos = TOPICS[cmd_key]
            client.subscribe(topic, qos=qos)
            log.info("Souscrit : %s (QoS %d)", topic, qos)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties,
    ) -> None:
        self._connected = False
        log.warning(
            "MQTT déconnecté (reason_code=%s) — reconnexion automatique…",
            reason_code,
        )

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        """Réception des commandes depuis le dashboard ou l'API REST."""
        topic   = message.topic
        payload = message.payload.decode("utf-8", errors="replace").strip()
        log.info("Commande reçue [%s] : %s", topic, payload)
        # Les commandes sont traitées par gnl_main.py
        # via retour dans la boucle principale (pattern callback → queue)
