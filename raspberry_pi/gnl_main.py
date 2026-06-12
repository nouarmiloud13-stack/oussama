#!/usr/bin/env python3
"""
gnl_main.py — Point d'entrée principal du système IoT GNL
Raspberry Pi 4 / GitHub Codespaces — Edge Computing Node

Modes :
  - SERIAL_PORT=/dev/ttyUSBx  → lecture directe Arduino (RPi physique)
  - SERIAL_PORT=SIMULATED     → lecture MQTT gnl/sim/raw (bridge PC → Codespaces)
"""

import os
import queue
import sys
import time
import json
import logging
import signal
import threading
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).parent))
from ai.anomaly_engine      import AnomalyEngine
from ai.gnl_smart_ai        import GNLSmartAI
from mqtt.mqtt_client       import GNLMQTTClient
from database.influx_writer import InfluxWriter
from database.mongo_writer  import MongoWriter
from api.rest_server        import start_api_server, update_latest, update_smart_diagnostic, set_mongo
from watchdog.gnl_watchdog  import SystemWatchdog

# ── Logs ───────────────────────────────────────────────────────────────────────
_DEFAULT_LOG = Path(__file__).parent.parent / "logs"
_LOG_DIR = Path(os.environ.get("GNL_LOG_DIR", str(_DEFAULT_LOG)))
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    _LOG_DIR = Path("./logs")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "gnl_main.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(str(_LOG_FILE)),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("gnl.main")

# ── Config ─────────────────────────────────────────────────────────────────────
SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "9600"))

MQTT_HOST   = os.environ.get("MQTT_HOST",          "localhost")
MQTT_PORT   = int(os.environ.get("MQTT_PORT",       "1883"))
MQTT_USER   = os.environ.get("MQTT_USER_PUBLISHER", "gnl_publisher")
MQTT_PASS   = os.environ.get("MQTT_PASS_PUBLISHER", "GNL_Secure_2025!")
MQTT_PUBLIC = os.environ.get("MQTT_PUBLIC", "false").lower() == "true"

SIM_TOPIC         = "gnl/sim/raw"
RECONNECT_S       = 5
LOOP_SLEEP        = 0.05
SIM_QUEUE_MAXSIZE = 100

# ── Arrêt propre ───────────────────────────────────────────────────────────────
_running = True


def _shutdown(sig, frame):
    global _running
    log.info("Signal %s reçu — arrêt propre…", sig)
    _running = False


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


# ══════════════════════════════════════════════════════════════════════════════
# MODE SÉRIE
# ══════════════════════════════════════════════════════════════════════════════

def open_serial():
    import serial
    while _running:
        try:
            ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=2)
            log.info("Port série ouvert : %s @ %d baud", SERIAL_PORT, SERIAL_BAUD)
            return ser
        except Exception as exc:
            log.warning("Port série indisponible (%s) — retry dans %ds", exc, RECONNECT_S)
            time.sleep(RECONNECT_S)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# MODE BRIDGE (Arduino réel sur PC → MQTT → Codespaces)
# ══════════════════════════════════════════════════════════════════════════════

_sim_queue: queue.Queue = queue.Queue(maxsize=SIM_QUEUE_MAXSIZE)


def _build_sim_subscriber() -> mqtt.Client:
    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(SIM_TOPIC, qos=0)
            log.info("Mode BRIDGE — abonné à '%s' sur %s:%d", SIM_TOPIC, MQTT_HOST, MQTT_PORT)
        else:
            log.error("Mode BRIDGE — connexion MQTT refusée (rc=%s)", reason_code)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.warning("Mode BRIDGE — déconnexion MQTT (rc=%s) — reconnexion auto…", reason_code)

    def on_message(client, userdata, msg):
        try:
            _sim_queue.put_nowait(msg.payload.decode("utf-8"))
        except queue.Full:
            log.debug("Mode BRIDGE — queue pleine, message ignoré")
        except Exception as exc:
            log.warning("Mode BRIDGE — erreur décodage : %s", exc)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="gnl_edge_sim_sub",
    )
    if not MQTT_PUBLIC:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    return client


def _connect_sim_subscriber(client: mqtt.Client) -> None:
    while _running:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_start()
            log.info("Mode BRIDGE — client MQTT connecté à %s:%d", MQTT_HOST, MQTT_PORT)
            return
        except Exception as exc:
            log.warning("Mode BRIDGE — connexion MQTT échouée (%s) — retry dans %ds", exc, RECONNECT_S)
            time.sleep(RECONNECT_S)


def start_sim_subscriber() -> mqtt.Client:
    client = _build_sim_subscriber()
    threading.Thread(
        target=_connect_sim_subscriber,
        args=(client,),
        daemon=True,
        name="gnl-bridge-subscriber",
    ).start()
    return client


# ══════════════════════════════════════════════════════════════════════════════
# PARSING JSON
# ══════════════════════════════════════════════════════════════════════════════

def parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
        required = {"n1", "n2", "t1", "t2", "p", "g"}
        if not required.issubset(data.keys()):
            log.debug("JSON incomplet (champs manquants) : %s", line)
            return None
        return data
    except json.JSONDecodeError as exc:
        log.debug("JSON invalide : %s — %s", line, exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE IA / MQTT / INFLUX / MONGO
# ══════════════════════════════════════════════════════════════════════════════

def process(
    data: dict,
    ai_engine: AnomalyEngine,
    smart_ai: GNLSmartAI,
    mqtt_client: GNLMQTTClient,
    influx: InfluxWriter,
    mongo: "MongoWriter | None" = None,
    watchdog: "SystemWatchdog | None" = None,
    ser=None,
) -> None:
    # 1. AnomalyEngine
    anomaly_result = ai_engine.analyze(data)
    data["ai"]     = anomaly_result

    # 2. Smart AI
    smart_result     = smart_ai.analyze(data, anomaly_result)
    data["smart_ai"] = smart_result
    update_smart_diagnostic(smart_result)
    update_latest(data)

    # 3. MQTT + InfluxDB + MongoDB
    mqtt_client.publish_all(data)
    influx.write(data)

    if mongo:
        mongo.write_reading(data)
        if smart_result.get("severity") in ("DANGER", "CRITIQUE"):
            mongo.write_diagnostic(smart_result, data)
        risk = anomaly_result.get("global_risk", 0)
        if risk >= 60 or data.get("err", 0):
            mongo.write_event(
                "ANOMALIE",
                f"risk={risk} err={data.get('err',0)}",
                {"n1": data.get("n1"), "n2": data.get("n2"), "g": data.get("g")},
            )

    # 4. Commandes Arduino (port série uniquement)
    if ser is not None:
        smart_cmds   = smart_result.get("commands", [])
        fallback_cmd = anomaly_result.get("command")
        sent = set()
        for cmd in smart_cmds:
            if cmd and cmd not in sent:
                ser.write((cmd + "\n").encode())
                log.warning("SmartAI → Arduino : %s", cmd)
                sent.add(cmd)
                if mongo:
                    mongo.write_event(cmd, "SmartAI", data)
        if not sent and fallback_cmd:
            ser.write((fallback_cmd + "\n").encode())
            log.warning("AnomalyEngine → Arduino : %s", fallback_cmd)
            if mongo:
                mongo.write_event(fallback_cmd, "AnomalyEngine", data)
        risk = anomaly_result.get("global_risk", 0)
        ser.write(f"RISK:{risk}\n".encode())

    # 5. Watchdog
    if watchdog is not None:
        watchdog.data_received(data)
        watchdog.pipeline_ok()


# ══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log.info("=== Démarrage Nouarmiloud Edge Node ===")
    log.info("SERIAL_PORT=%s | MQTT_HOST=%s:%d", SERIAL_PORT, MQTT_HOST, MQTT_PORT)

    ai_engine   = AnomalyEngine()
    smart_ai    = GNLSmartAI()
    mqtt_client = GNLMQTTClient()
    influx      = InfluxWriter()
    mongo       = MongoWriter()
    set_mongo(mongo)

    if smart_ai.is_available:
        log.info("Smart AI activé (Gemma4 @ %s)", os.environ.get("GEMMA4_HOST", "localhost"))
    else:
        log.warning("Gemma4 non disponible — mode AnomalyEngine seul")

    def _request_stop():
        global _running
        _running = False

    watchdog = SystemWatchdog(
        shutdown_flag_setter=_request_stop,
        mqtt_client=mqtt_client,
    )

    threading.Thread(target=start_api_server, daemon=True, name="gnl-api").start()
    log.info("API REST démarrée (thread daemon)")

    mqtt_client.connect()
    watchdog.start()

    if SERIAL_PORT == "SIMULATED":
        log.info("Mode BRIDGE activé — en attente des données Arduino via PC bridge")
        log.info("Sur ton PC : python arduino_serial_bridge.py --port COM3 --host broker.hivemq.com --public")
        _run_simulated(ai_engine, smart_ai, mqtt_client, influx, watchdog, mongo=mongo)
    else:
        _run_serial(ai_engine, smart_ai, mqtt_client, influx, watchdog, mongo=mongo)

    watchdog.stop()
    mqtt_client.disconnect()
    influx.close()
    mongo.close()
    log.info("=== Nouarmiloud Edge Node arrêté proprement ===")


def _run_simulated(
    ai_engine, smart_ai, mqtt_client, influx, watchdog=None, mongo=None,
) -> None:
    sim_client = start_sim_subscriber()
    while _running:
        try:
            raw = _sim_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        data = parse_line(raw)
        if data is None:
            continue
        try:
            process(data, ai_engine, smart_ai, mqtt_client, influx,
                    mongo=mongo, watchdog=watchdog, ser=None)
        except Exception as exc:
            log.exception("Erreur traitement données bridge : %s", exc)
            if watchdog:
                watchdog.pipeline_error()
    try:
        sim_client.loop_stop()
        sim_client.disconnect()
    except Exception:
        pass
    log.info("Mode BRIDGE — abonné MQTT arrêté")


def _run_serial(
    ai_engine, smart_ai, mqtt_client, influx, watchdog=None, mongo=None,
) -> None:
    log.info("Mode SÉRIE activé — port %s @ %d baud", SERIAL_PORT, SERIAL_BAUD)
    ser = None
    while _running:
        if ser is None or not ser.is_open:
            ser = open_serial()
            if ser is None:
                break
            if watchdog:
                watchdog.set_serial(ser)
        try:
            raw  = ser.readline().decode("utf-8", errors="replace")
            data = parse_line(raw)
            if data is None:
                time.sleep(LOOP_SLEEP)
                continue
            process(data, ai_engine, smart_ai, mqtt_client, influx,
                    mongo=mongo, watchdog=watchdog, ser=ser)
        except OSError as exc:
            log.error("Erreur série : %s — reconnexion…", exc)
            if watchdog:
                watchdog.set_serial(None)
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(RECONNECT_S)
        except Exception as exc:
            log.exception("Erreur inattendue : %s", exc)
            if watchdog:
                watchdog.pipeline_error()
        time.sleep(LOOP_SLEEP)
    if ser and ser.is_open:
        ser.close()
    log.info("Mode SÉRIE — port fermé")


if __name__ == "__main__":
    main()
