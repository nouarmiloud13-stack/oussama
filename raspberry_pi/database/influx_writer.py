#!/usr/bin/env python3
"""
influx_writer.py — Écriture des données IoT dans InfluxDB 2.x

Base de données : Time Series (InfluxDB)
Rétention : 30 jours (configurée dans InfluxDB)
Organisation : gnl_org
Bucket : gnl_monitoring

Mesurements :
  - niveau       : niveau_r1, niveau_r2 (%)
  - temperature  : temp_r1, temp_r2 (°C)
  - gaz          : valeur brute MQ-4 (ADC), niveau texte
  - pression     : hPa
  - ia           : scores Isolation Forest, risque global
  - actuateurs   : état pompe, vanne (0/1)
  - alertes      : log des événements critiques
"""

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("gnl.influx")

# ── Configuration ───────────────────────────────────────────────────────────────
INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = "gnl_influx_token_secret_2025"   # à remplacer après setup
INFLUX_ORG    = "gnl_org"
INFLUX_BUCKET = "gnl_monitoring"

# Rétention des données
RETENTION_DAYS = 30

RETRY_MAX   = 3
RETRY_DELAY = 1.0


class InfluxWriter:
    """Écrit les données capteurs dans InfluxDB 2.x via l'API Python officielle."""

    def __init__(self):
        self._client = None
        self._write_api = None
        self._connected = False
        self._connect()

    def _connect(self):
        try:
            from influxdb_client import InfluxDBClient, WriteOptions
            from influxdb_client.client.write_api import SYNCHRONOUS

            self._client = InfluxDBClient(
                url=INFLUX_URL,
                token=INFLUX_TOKEN,
                org=INFLUX_ORG,
            )
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
            self._connected = True
            log.info("InfluxDB connecté : %s / bucket=%s", INFLUX_URL, INFLUX_BUCKET)
        except ImportError:
            log.error("influxdb-client non installé — pip3 install influxdb-client")
        except Exception as e:
            log.error("InfluxDB connexion échouée : %s", e)

    def write(self, data: dict):
        """
        Écrit une mesure complète dans InfluxDB.
        data = dict Arduino enrichi avec ai : {n1, n2, t1, t2, p, g, pump, valve, ai:{...}}
        """
        if not self._connected:
            return

        now = datetime.now(timezone.utc)
        ai  = data.get("ai", {})

        # Construction des points Line Protocol
        points = self._build_points(data, ai, now)

        for attempt in range(RETRY_MAX):
            try:
                self._write_api.write(
                    bucket=INFLUX_BUCKET,
                    org=INFLUX_ORG,
                    record=points,
                )
                log.debug("InfluxDB : %d points écrits", len(points))
                return
            except Exception as e:
                log.warning("InfluxDB write error (tentative %d/%d) : %s", attempt+1, RETRY_MAX, e)
                time.sleep(RETRY_DELAY)

        log.error("InfluxDB : écriture échouée après %d tentatives", RETRY_MAX)

    def _build_points(self, data: dict, ai: dict, ts) -> list:
        """Construit la liste des points InfluxDB au format dict."""
        from influxdb_client.domain.write_precision import WritePrecision
        from influxdb_client import Point

        reg = ai.get("regression", {})
        points = []

        # ── Niveaux ──
        points.append(
            Point("niveau")
            .tag("reservoir", "R1")
            .field("valeur", float(data.get("n1", 0)))
            .field("prediction_30s", reg.get("n1_in_30s") or 0.0)
            .time(ts)
        )
        points.append(
            Point("niveau")
            .tag("reservoir", "R2")
            .field("valeur", float(data.get("n2", 0)))
            .field("prediction_30s", reg.get("n2_in_30s") or 0.0)
            .time(ts)
        )

        # ── Températures ──
        points.append(
            Point("temperature")
            .tag("capteur", "DS18B20_R1")
            .field("valeur", float(data.get("t1", 0)))
            .time(ts)
        )
        points.append(
            Point("temperature")
            .tag("capteur", "DS18B20_R2")
            .field("valeur", float(data.get("t2", 0)))
            .time(ts)
        )

        # ── Gaz MQ-4 ──
        points.append(
            Point("gaz")
            .tag("capteur", "MQ4")
            .tag("niveau", self._gas_level_str(data.get("g", 0)))
            .field("valeur_adc", int(data.get("g", 0)))
            .time(ts)
        )

        # ── Pression BMP280 ──
        points.append(
            Point("pression")
            .tag("capteur", "BMP280")
            .field("valeur_hpa", float(data.get("p", 1013)))
            .time(ts)
        )

        # ── Actuateurs ──
        points.append(
            Point("actuateurs")
            .field("pompe",  int(data.get("pump",  0)))
            .field("vanne",  int(data.get("valve", 0)))
            .time(ts)
        )

        # ── IA scores ──
        points.append(
            Point("ia_scores")
            .field("isolation_forest", int(ai.get("isolation_forest", 0)))
            .field("global_risk",      int(ai.get("global_risk", 0)))
            .field("overflow_risk",    int(reg.get("overflow_risk", False)))
            .time(ts)
        )

        # ── Alerte (si présente) ──
        gas_alert = ai.get("gas_alert")
        if gas_alert:
            points.append(
                Point("alertes")
                .tag("type", gas_alert)
                .tag("source", "MQ4")
                .field("valeur", int(data.get("g", 0)))
                .field("global_risk", int(ai.get("global_risk", 0)))
                .time(ts)
            )

        return points

    @staticmethod
    def _gas_level_str(gas: int) -> str:
        if gas < 250:
            return "OK"
        if gas < 450:
            return "ATTENTION"
        return "DANGER"

    def close(self):
        if self._client:
            self._client.close()
            log.info("InfluxDB connexion fermée")

    def query_last_n(self, measurement: str, n: int = 60) -> list:
        """Requête des N dernières mesures (pour l'API REST)."""
        if not self._connected:
            return []
        try:
            query_api = self._client.query_api()
            flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{n * 2}s)
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> last()
'''
            result = query_api.query(flux, org=INFLUX_ORG)
            rows = []
            for table in result:
                for record in table.records:
                    rows.append({
                        "time":  record.get_time().isoformat(),
                        "field": record.get_field(),
                        "value": record.get_value(),
                    })
            return rows
        except Exception as e:
            log.warning("InfluxDB query error : %s", e)
            return []
