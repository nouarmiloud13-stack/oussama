#!/usr/bin/env python3
"""
arduino_serial_bridge.py — Bridge Arduino réel (PC) → MQTT → Codespaces

Lit les données JSON envoyées par l'Arduino via USB/série sur le PC,
puis les publie sur le topic MQTT gnl/sim/raw accessible depuis Codespaces.

Usage :
  python arduino_serial_bridge.py --port COM3
  python arduino_serial_bridge.py --port COM3 --baud 9600 --host localhost --mqtt-port 1883
"""

import argparse
import logging
import sys
import time

import paho.mqtt.client as mqtt
import serial

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("gnl.bridge")

SIM_TOPIC = "gnl/sim/raw"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bridge Arduino série → MQTT Codespaces")
    p.add_argument("--port",      required=True,          help="Port série Arduino (ex: COM3, /dev/ttyUSB0)")
    p.add_argument("--baud",      type=int, default=9600, help="Vitesse série (défaut: 9600)")
    p.add_argument("--host",      default="localhost",    help="Hôte MQTT (défaut: localhost)")
    p.add_argument("--mqtt-port", type=int, default=1883, help="Port MQTT (défaut: 1883)")
    p.add_argument("--user",      default="gnl_publisher", help="Utilisateur MQTT")
    p.add_argument("--password",  default="GNL_Secure_2025!", help="Mot de passe MQTT")
    p.add_argument("--public",    action="store_true",    help="MQTT sans authentification")
    return p.parse_args()


def build_mqtt_client(args: argparse.Namespace) -> mqtt.Client:
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connecté à %s:%d", args.host, args.mqtt_port)
        else:
            log.error("MQTT connexion refusée (rc=%d)", rc)

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning("MQTT déconnecté (rc=%d) — reconnexion auto…", rc)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
        client_id="gnl_arduino_bridge",
        clean_session=True,
    )
    if not args.public:
        client.username_pw_set(args.user, args.password)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def run(args: argparse.Namespace) -> None:
    mqtt_client = build_mqtt_client(args)

    log.info("Connexion MQTT → %s:%d …", args.host, args.mqtt_port)
    mqtt_client.connect(args.host, args.mqtt_port, keepalive=60)
    mqtt_client.loop_start()

    log.info("Ouverture port série %s @ %d baud …", args.port, args.baud)
    ser = serial.Serial(args.port, args.baud, timeout=2)
    log.info("Bridge démarré — Arduino → MQTT topic '%s'", SIM_TOPIC)
    log.info("Ctrl+C pour arrêter")

    try:
        while True:
            try:
                raw = ser.readline().decode("utf-8", errors="replace").strip()
            except serial.SerialException as exc:
                log.error("Erreur série : %s", exc)
                time.sleep(2)
                continue

            if not raw:
                continue

            if not raw.startswith("{"):
                log.debug("Ligne ignorée (non-JSON) : %s", raw)
                continue

            result = mqtt_client.publish(SIM_TOPIC, raw, qos=0)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                log.info("→ MQTT : %s", raw)
            else:
                log.warning("Échec publication MQTT (rc=%d) : %s", result.rc, raw)

    except KeyboardInterrupt:
        log.info("Arrêt demandé")
    finally:
        ser.close()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        log.info("Bridge arrêté proprement")


if __name__ == "__main__":
    run(parse_args())
