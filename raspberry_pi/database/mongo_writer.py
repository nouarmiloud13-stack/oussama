#!/usr/bin/env python3
"""
mongo_writer.py — Historique GNL dans MongoDB

Collections :
  readings    : mesures capteurs (TTL 30 jours)
  diagnostics : rapports IA      (TTL 30 jours)
  events      : alertes/ESD/actions (TTL 30 jours)

Usage :
  from database.mongo_writer import MongoWriter
  mongo = MongoWriter()
  mongo.write_reading(data)
  history = mongo.get_today_history()
"""

import os
import time
import logging
from datetime import datetime, timezone

from pymongo import MongoClient, DESCENDING
from pymongo.errors import ServerSelectionTimeoutError, PyMongoError

log = logging.getLogger("gnl.mongo")

MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb://gnl_admin:GNL_Mongo_2025!@mongodb:27017/"
)
MONGO_DB  = os.environ.get("MONGO_DB", "gnl_history")
TTL_DAYS  = int(os.environ.get("MONGO_TTL_DAYS", "30"))


_RECONNECT_INTERVAL = 30  # secondes entre deux tentatives de reconnexion


class MongoWriter:
    def __init__(self):
        self._client        = None
        self._db            = None
        self._last_attempt  = 0.0
        self._connect()

    # ── Connexion ──────────────────────────────────────────────────────────────

    def _connect(self):
        self._last_attempt = time.time()
        try:
            self._client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            self._client.admin.command("ping")
            self._db = self._client[MONGO_DB]
            ttl_s = TTL_DAYS * 24 * 3600
            for col in ("readings", "diagnostics", "events"):
                self._db[col].create_index("ts", expireAfterSeconds=ttl_s)
            log.info("MongoDB connecté → base '%s' (TTL %d jours)", MONGO_DB, TTL_DAYS)
        except (ServerSelectionTimeoutError, PyMongoError, Exception) as exc:
            log.warning("MongoDB non disponible (%s) — historique désactivé", exc)
            self._db = None

    @property
    def available(self) -> bool:
        if self._db is not None:
            return True
        # Retry automatique toutes les 30 s (si MongoDB démarre après Flask)
        if time.time() - self._last_attempt > _RECONNECT_INTERVAL:
            log.info("MongoDB : tentative de reconnexion...")
            self._connect()
        return self._db is not None

    # ── Écriture ───────────────────────────────────────────────────────────────

    def write_reading(self, data: dict):
        """Enregistre une lecture capteur (appelé à chaque cycle de 2 s)."""
        if not self.available:
            return
        try:
            self._db.readings.insert_one({
                "ts":    datetime.now(timezone.utc),
                "n1":    data.get("n1"),
                "n2":    data.get("n2"),
                "t1":    data.get("t1"),
                "t2":    data.get("t2"),
                "p":     data.get("p"),
                "g":     data.get("g"),
                "pump":  data.get("pump", 0),
                "valve": data.get("valve", 0),
                "err":   data.get("err", 0),
            })
        except PyMongoError as exc:
            log.debug("MongoDB write_reading : %s", exc)

    def write_diagnostic(self, smart_result: dict, sensor_data: dict):
        """Enregistre un rapport complet du Smart AI."""
        if not self.available:
            return
        try:
            self._db.diagnostics.insert_one({
                "ts":       datetime.now(timezone.utc),
                "risk":     smart_result.get("global_risk", 0),
                "status":   smart_result.get("status", "unknown"),
                "severity": smart_result.get("severity", "INFO"),
                "message":  smart_result.get("message", ""),
                "diagnostic": smart_result.get("diagnostic", ""),
                "commands": smart_result.get("commands", []),
                "source":   smart_result.get("source", "smart_ai"),
                "n1":  sensor_data.get("n1"),
                "n2":  sensor_data.get("n2"),
                "g":   sensor_data.get("g"),
            })
        except PyMongoError as exc:
            log.debug("MongoDB write_diagnostic : %s", exc)

    def write_event(self, event_type: str, detail: str = "", data: dict = None):
        """
        Enregistre un évènement remarquable.
        event_type: "ESD", "GAS_ALARM", "PUMP_ON", "PUMP_OFF",
                    "VALVE_OPEN", "VALVE_CLOSE", "SENSOR_ERROR", ...
        """
        if not self.available:
            return
        try:
            self._db.events.insert_one({
                "ts":     datetime.now(timezone.utc),
                "type":   event_type,
                "detail": detail,
                "data":   data or {},
            })
        except PyMongoError as exc:
            log.debug("MongoDB write_event : %s", exc)

    # ── Lecture historique du jour ─────────────────────────────────────────────

    def get_today_history(self, limit: int = 120) -> list[dict]:
        """
        Retourne les dernières lectures du jour courant.
        Utilisé par le Smart AI pour contextualiser ses analyses.
        """
        if not self.available:
            return []
        try:
            today = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            cursor = (
                self._db.readings
                .find({"ts": {"$gte": today}}, {"_id": 0})
                .sort("ts", DESCENDING)
                .limit(limit)
            )
            docs = list(cursor)
            for d in docs:
                if "ts" in d:
                    d["ts"] = d["ts"].isoformat()
            return docs
        except PyMongoError as exc:
            log.debug("MongoDB get_today_history : %s", exc)
            return []

    def get_today_diagnostics(self, limit: int = 20) -> list[dict]:
        """Retourne les N derniers diagnostics IA du jour."""
        if not self.available:
            return []
        try:
            today = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            cursor = (
                self._db.diagnostics
                .find({"ts": {"$gte": today}}, {"_id": 0})
                .sort("ts", DESCENDING)
                .limit(limit)
            )
            docs = list(cursor)
            for d in docs:
                if "ts" in d:
                    d["ts"] = d["ts"].isoformat()
            return docs
        except PyMongoError as exc:
            log.debug("MongoDB get_today_diagnostics : %s", exc)
            return []

    def get_today_events(self) -> list[dict]:
        """Retourne tous les évènements (alertes, ESD) du jour."""
        if not self.available:
            return []
        try:
            today = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            cursor = (
                self._db.events
                .find({"ts": {"$gte": today}}, {"_id": 0})
                .sort("ts", DESCENDING)
            )
            docs = list(cursor)
            for d in docs:
                if "ts" in d:
                    d["ts"] = d["ts"].isoformat()
            return docs
        except PyMongoError as exc:
            log.debug("MongoDB get_today_events : %s", exc)
            return []

    def get_daily_summary(self, date: datetime | None = None) -> dict:
        """
        Résumé statistique d'une journée (min/max/moy capteurs + nb alertes).
        date = None → aujourd'hui.
        """
        if not self.available:
            return {}
        try:
            if date is None:
                date = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            next_day = date.replace(hour=23, minute=59, second=59)
            pipeline = [
                {"$match": {"ts": {"$gte": date, "$lte": next_day}}},
                {"$group": {
                    "_id":    None,
                    "count":  {"$sum": 1},
                    "n1_min": {"$min": "$n1"}, "n1_max": {"$max": "$n1"},
                    "n1_avg": {"$avg": "$n1"},
                    "n2_min": {"$min": "$n2"}, "n2_max": {"$max": "$n2"},
                    "n2_avg": {"$avg": "$n2"},
                    "g_max":  {"$max": "$g"},
                    "g_avg":  {"$avg": "$g"},
                    "t1_avg": {"$avg": "$t1"},
                    "pump_activations": {"$sum": "$pump"},
                    "valve_activations": {"$sum": "$valve"},
                }},
            ]
            result = list(self._db.readings.aggregate(pipeline))
            nb_events = self._db.events.count_documents(
                {"ts": {"$gte": date, "$lte": next_day}}
            )
            summary = result[0] if result else {}
            summary.pop("_id", None)
            summary["nb_events"] = nb_events
            summary["date"] = date.date().isoformat()
            return summary
        except PyMongoError as exc:
            log.debug("MongoDB get_daily_summary : %s", exc)
            return {}

    # ── Fermeture ──────────────────────────────────────────────────────────────

    def close(self):
        if self._client:
            self._client.close()
            log.info("MongoDB connexion fermée")
