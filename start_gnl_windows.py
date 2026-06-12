#!/usr/bin/env python3
"""
start_gnl_windows.py — Lanceur standalone Windows
Fonctionne SANS Docker, SANS MQTT, SANS InfluxDB
- Lit l'Arduino via port COM (ex: COM3) OU simule des données réalistes
- Stocke l'historique dans SQLite local (gnl_history.db)
- Lance le serveur Flask et ouvre le navigateur automatiquement

Usage:
  python start_gnl_windows.py                  # simulation
  python start_gnl_windows.py --port COM3      # Arduino réel
  python start_gnl_windows.py --port COM3 --baud 9600
"""

import os
import sys
import json
import math
import time
import random
import sqlite3
import logging
import argparse
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# Ajouter raspberry_pi au chemin Python
_BASE = Path(__file__).parent
sys.path.insert(0, str(_BASE / "raspberry_pi"))

os.environ.setdefault("API_HOST",  "0.0.0.0")
os.environ.setdefault("API_PORT",  "5000")
os.environ.setdefault("MQTT_HOST", "disabled")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("gnl.win")

# ══════════════════════════════════════════════════════════════════════════════
# BASE DE DONNÉES SQLITE (remplace MongoDB sans Docker)
# ══════════════════════════════════════════════════════════════════════════════

class SQLiteHistory:
    """Historique local SQLite — même interface que MongoWriter."""

    def __init__(self, db_path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init()
        self.available = True
        log.info("SQLite ouvert : %s", db_path)

    def _init(self):
        c = self._conn
        c.execute("""CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            n1 REAL, n2 REAL, t1 REAL, t2 REAL,
            p REAL, g INTEGER, pump INTEGER, valve INTEGER, err INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            severity TEXT, diagnostic TEXT, commands TEXT,
            source TEXT, n1 REAL, n2 REAL, g INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            type TEXT, detail TEXT, data TEXT)""")
        c.commit()

    # ── Écriture ───────────────────────────────────────────────────────────────

    def write_reading(self, data: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO readings (ts,n1,n2,t1,t2,p,g,pump,valve,err) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(),
                 data.get("n1"), data.get("n2"),
                 data.get("t1"), data.get("t2"),
                 data.get("p"), data.get("g"),
                 data.get("pump", 0), data.get("valve", 0), data.get("err", 0)))
            self._conn.commit()

    def write_diagnostic(self, smart_result: dict, sensor_data: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO diagnostics (ts,severity,diagnostic,commands,source,n1,n2,g) VALUES (?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(),
                 smart_result.get("severity", "INFO"),
                 smart_result.get("diagnostic", ""),
                 json.dumps(smart_result.get("commands", [])),
                 smart_result.get("source", "smart_ai"),
                 sensor_data.get("n1"), sensor_data.get("n2"), sensor_data.get("g")))
            self._conn.commit()

    def write_event(self, event_type: str, detail: str = "", data: dict = None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts,type,detail,data) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(),
                 event_type, detail, json.dumps(data or {})))
            self._conn.commit()

    # ── Lecture ────────────────────────────────────────────────────────────────

    def get_today_history(self, limit: int = 120) -> list:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts,n1,n2,t1,t2,p,g,pump,valve,err FROM readings WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                (today, limit))
            cols = ["ts","n1","n2","t1","t2","p","g","pump","valve","err"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_today_diagnostics(self, limit: int = 20) -> list:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts,severity,diagnostic,commands,source,n1,n2,g FROM diagnostics WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                (today, limit))
            cols = ["ts","severity","diagnostic","commands","source","n1","n2","g"]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            try:
                r["commands"] = json.loads(r["commands"])
            except Exception:
                r["commands"] = []
        return rows

    def get_today_events(self) -> list:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts,type,detail,data FROM events WHERE ts>=? ORDER BY ts DESC",
                (today,))
            cols = ["ts","type","detail","data"]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            try:
                r["data"] = json.loads(r["data"])
            except Exception:
                r["data"] = {}
        return rows

    def get_daily_summary(self, date=None) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            cur = self._conn.execute("""
                SELECT COUNT(*), MIN(n1),MAX(n1),AVG(n1),
                       MIN(n2),MAX(n2),AVG(n2),
                       MAX(g),AVG(g),AVG(t1),SUM(pump),SUM(valve)
                FROM readings WHERE ts>=?""", (today,))
            row = cur.fetchone()
            nb_ev = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE ts>=?", (today,)).fetchone()[0]
        if not row or not row[0]:
            return {}
        return {
            "date": today, "count": row[0],
            "n1_min": row[1], "n1_max": row[2], "n1_avg": row[3],
            "n2_min": row[4], "n2_max": row[5], "n2_avg": row[6],
            "g_max": row[7], "g_avg": row[8], "t1_avg": row[9],
            "pump_activations": row[10], "valve_activations": row[11],
            "nb_events": nb_ev,
        }

    def close(self):
        self._conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATEUR ARDUINO RÉALISTE
# ══════════════════════════════════════════════════════════════════════════════

class ArduinoSimulator:
    """Simule un Arduino avec physique réaliste (pompe, vanne, capteurs, gaz)."""

    def __init__(self):
        self._t      = 0
        self.n1      = 78.0   # Niveau réservoir 1 (%)
        self.n2      = 22.0   # Niveau réservoir 2 (%)
        self.pump    = 0      # Pompe (0=OFF, 1=ON)
        self.valve   = 0      # Vanne (0=FERMÉE, 1=OUVERTE)
        self._gas_spike     = False
        self._gas_spike_cnt = 0

    def receive_command(self, cmd: str):
        if   cmd == "CMD:PUMP_ON":    self.pump  = 1
        elif cmd == "CMD:PUMP_OFF":   self.pump  = 0
        elif cmd == "CMD:VALVE_OPEN": self.valve = 1
        elif cmd == "CMD:VALVE_CLOSE":self.valve = 0
        elif cmd == "CMD:ESD":
            self.pump = 0; self.valve = 0

    def read(self) -> dict:
        self._t += 1
        dt = 2.0  # secondes entre deux lectures

        # ── Physique des niveaux ──
        debit_pompe  = 0.15   # % par seconde si pompe ON
        debit_vanne  = 0.12   # % par seconde si vanne ouverte

        if self.pump and self.n1 > 10:
            self.n1 -= debit_pompe * dt + random.gauss(0, 0.03)
        if self.pump and self.valve and self.n2 < 100:
            self.n2 += debit_vanne * dt + random.gauss(0, 0.03)

        # Alimentation externe R1 (simulation réseau d'alimentation)
        if self.n1 < 30:
            self.n1 += 0.10 * dt  # remplissage lent

        # ── Auto-régulation (Arduino firmware logic) ──
        if self.n1 > 90 and not self.pump:
            self.pump = 1; self.valve = 1  # démarrage automatique
        if self.n2 >= 95:
            self.valve = 0; self.pump = 0  # réservoir plein
        if self.n1 <= 10:
            self.pump = 0                  # protection anti-cavitation

        self.n1 = max(5.0, min(100.0, self.n1))
        self.n2 = max(0.0, min(100.0, self.n2))

        # ── Température DS18B20 ──
        t1 = 21.5 + 1.8 * math.sin(self._t / 25.0) + random.gauss(0, 0.08)
        t2 = -127.0  # capteur non branché sur R2

        # ── Pression BMP280 ──
        p = 1013.25 + 0.8 * math.sin(self._t / 60.0) + random.gauss(0, 0.3)

        # ── Gaz MQ-4 (avec pics occasionnels réalistes) ──
        if random.random() < 0.004:
            self._gas_spike = True
            self._gas_spike_cnt = random.randint(3, 10)
        if self._gas_spike:
            g = random.randint(270, 510)
            self._gas_spike_cnt -= 1
            if self._gas_spike_cnt <= 0:
                self._gas_spike = False
        else:
            g = int(115 + 18 * math.sin(self._t / 40.0) + random.gauss(0, 12))
            g = max(70, min(210, g))

        return {
            "n1":   round(self.n1, 1),
            "n2":   round(self.n2, 1),
            "t1":   round(t1, 1),
            "t2":   t2,
            "p":    round(p, 1),
            "g":    g,
            "pump": self.pump,
            "valve": self.valve,
            "err":  0,
        }


# ══════════════════════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════════════════════════════

def _main_loop(ai_engine, smart_ai, db: SQLiteHistory,
               simulator=None, ser=None, use_serial=False):
    from api.rest_server import update_latest, update_smart_diagnostic, pop_command

    log.info("Boucle données démarrée (mode %s)", "SÉRIE" if use_serial else "SIMULATION")

    while True:
        try:
            # ── Lecture données ──
            if use_serial:
                raw = ser.readline().decode("utf-8", errors="replace").strip()
                if not raw.startswith("{"):
                    time.sleep(0.05)
                    continue
                try:
                    data = json.loads(raw)
                    required = {"n1", "n2", "t1", "t2", "p", "g"}
                    if not required.issubset(data.keys()):
                        continue
                except Exception:
                    continue
            else:
                time.sleep(2.0)
                data = simulator.read()

            # ── Pipeline IA ──
            anomaly = ai_engine.analyze(data)
            data["ai"] = anomaly

            smart = smart_ai.analyze(data, anomaly)
            data["smart_ai"] = smart

            update_latest(data)
            update_smart_diagnostic(smart)

            # ── Sauvegarde SQLite ──
            db.write_reading(data)
            if smart.get("severity") in ("DANGER", "CRITIQUE"):
                db.write_diagnostic(smart, data)
            risk = anomaly.get("global_risk", 0)
            if risk >= 60 or data.get("err", 0):
                db.write_event("ANOMALIE", f"risk={risk}", {
                    "n1": data.get("n1"), "n2": data.get("n2"), "g": data.get("g")})

            # ── Commandes ──
            cmd = pop_command()
            if cmd:
                if use_serial:
                    ser.write((cmd + "\n").encode())
                elif simulator:
                    simulator.receive_command(cmd)
                db.write_event(cmd, "API_MANUEL", {})
                log.info("Commande exécutée : %s", cmd)

            # Commandes IA
            for cmd in smart.get("commands", []):
                if use_serial:
                    ser.write((cmd + "\n").encode())
                elif simulator:
                    simulator.receive_command(cmd)
                db.write_event(cmd, smart.get("source", "ia"), {})
                log.info("Commande IA : %s", cmd)

        except Exception as exc:
            log.exception("Erreur boucle principale : %s", exc)
            time.sleep(2.0)


# ══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="GNL Edge Monitor — Lanceur Windows")
    parser.add_argument("--port",  default="", help="Port COM Arduino (ex: COM3)")
    parser.add_argument("--baud",  type=int, default=9600, help="Vitesse série (défaut: 9600)")
    parser.add_argument("--no-browser", action="store_true", help="Ne pas ouvrir le navigateur")
    args = parser.parse_args()

    use_serial = bool(args.port)
    ser = None

    log.info("=" * 60)
    log.info("  GNL Edge Monitor — Windows Standalone")
    log.info("=" * 60)

    # ── Composants ──
    from ai.anomaly_engine import AnomalyEngine
    from ai.gnl_smart_ai   import GNLSmartAI
    from api.rest_server   import start_api_server, set_mongo

    ai_engine = AnomalyEngine()
    smart_ai  = GNLSmartAI()
    db        = SQLiteHistory(_BASE / "gnl_history.db")
    set_mongo(db)

    # ── Arduino série ou simulateur ──
    simulator = None
    if use_serial:
        import serial as pyserial
        log.info("Connexion Arduino sur %s @ %d baud…", args.port, args.baud)
        try:
            ser = pyserial.Serial(args.port, args.baud, timeout=2)
            log.info("Arduino connecté : %s", args.port)
        except Exception as exc:
            log.error("Impossible d'ouvrir %s : %s", args.port, exc)
            log.warning("Basculement en mode simulation")
            use_serial = False
            ser = None

    if not use_serial:
        simulator = ArduinoSimulator()
        log.info("Simulateur Arduino activé (données réalistes)")

    # ── API Flask en thread daemon ──
    api_thread = threading.Thread(target=start_api_server, daemon=True, name="gnl-api")
    api_thread.start()
    time.sleep(1.2)  # attendre que Flask démarre

    # ── Boucle données en thread daemon ──
    loop_thread = threading.Thread(
        target=_main_loop,
        args=(ai_engine, smart_ai, db, simulator, ser, use_serial),
        daemon=True,
        name="gnl-loop",
    )
    loop_thread.start()

    # ── Navigateur ──
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000")).start()

    log.info("")
    log.info("  Dashboard  →  http://localhost:5000")
    log.info("  Login      →  admin  /  admin_GNL_2025!")
    log.info("  Opérateur  →  operator  /  oper_GNL_2025!")
    log.info("  Historique →  gnl_history.db (SQLite)")
    log.info("  Arrêt      →  Ctrl+C")
    log.info("")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Arrêt demandé — fermeture propre…")
    finally:
        if ser:
            ser.close()
        db.close()
        log.info("GNL Edge Monitor arrêté.")


if __name__ == "__main__":
    main()
