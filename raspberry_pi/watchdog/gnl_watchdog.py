#!/usr/bin/env python3
"""
gnl_watchdog.py — Chien de garde de sécurité du système GNL

CONDITIONS D'ARRÊT (mode SÉRIE physique uniquement) :
  1. SILENCE_ARDUINO    : aucune donnée depuis > WATCHDOG_TIMEOUT_S
  2. SENSORS_DEAD       : HC-SR04 R1+R2 simultanément HS > N cycles
  3. GAS_CONFIRMED      : gaz > 450 ADC pendant > CONFIRM_GAS cycles
  4. ESD_UNACKNOWLEDGED : ESD envoyé, Arduino pas acquitté après 10s
  5. CASCADE_ERRORS     : > WATCHDOG_MAX_ERRORS exceptions consécutives

En mode SIMULATED (Codespaces / bridge PC) :
  - Le silence Arduino déclenche un WARNING uniquement, pas un arrêt.
  - Les autres conditions (gaz, capteurs, erreurs) restent actives.

ACTIONS EN CASCADE (mode série) :
  1. Envoyer CMD:ESD sur port série (direct)
  2. Publier alerte MQTT (best-effort)
  3. Logger l'événement
  4. Stopper la boucle principale
  5. Si WATCHDOG_OS_SHUTDOWN=true → sudo systemctl halt
"""

import os
import time
import logging
import subprocess
import threading
import json
from datetime import datetime, timezone

log = logging.getLogger("gnl.watchdog")

# ── Configuration ─────────────────────────────────────────────────────────────
WATCHDOG_TIMEOUT_S  = int(os.environ.get("WATCHDOG_TIMEOUT_S",  "60"))
WATCHDOG_MAX_ERRORS = int(os.environ.get("WATCHDOG_MAX_ERRORS", "10"))
WATCHDOG_TICK_S     = float(os.environ.get("WATCHDOG_TICK_S",   "2.0"))
WATCHDOG_OS_SHUTDOWN= os.environ.get("WATCHDOG_OS_SHUTDOWN", "false").lower() == "true"
WATCHDOG_CONFIRM_GAS= int(os.environ.get("CONFIRM_GAS", "3"))
WATCHDOG_SENSORS_MAX= int(os.environ.get("WATCHDOG_SENSORS_DEAD_MAX", "5"))
ESD_ACK_TIMEOUT_S   = int(os.environ.get("ESD_ACK_TIMEOUT_S",  "10"))

# Mode SIMULATED : pas d'arrêt sur silence Arduino
SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
IS_SIMULATED = SERIAL_PORT == "SIMULATED"


class SystemWatchdog:
    """
    Chien de garde du système GNL.

    Usage dans gnl_main.py :
        watchdog = SystemWatchdog(shutdown_flag_setter=lambda: set_running_false())
        watchdog.start()

        # Dans la boucle principale :
        watchdog.data_received(data)
        watchdog.pipeline_ok()
        watchdog.pipeline_error()
        watchdog.set_serial(ser)   # mode série uniquement
    """

    def __init__(self, shutdown_flag_setter, mqtt_client=None):
        self._stop_main      = shutdown_flag_setter
        self._mqtt           = mqtt_client
        self._ser            = None
        self._lock           = threading.Lock()

        self._last_data_ts:  float = time.time()
        self._error_count:   int   = 0
        self._gas_danger_n:  int   = 0
        self._sensors_dead_n:int   = 0
        self._esd_sent_ts:   float = 0.0
        self._esd_sent:      bool  = False

        # Compteur pour éviter le spam de logs en mode SIMULATED
        self._silence_warn_count: int = 0

        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            daemon=True,
            name="gnl-watchdog",
        )

    # ── API publique ──────────────────────────────────────────────────────────

    def start(self):
        self._thread.start()
        mode_str = "BRIDGE/SIMULATED (arrêt silence désactivé)" if IS_SIMULATED else "SÉRIE"
        log.info(
            "Watchdog démarré (timeout=%ds, max_err=%d, OS_shutdown=%s, mode=%s)",
            WATCHDOG_TIMEOUT_S, WATCHDOG_MAX_ERRORS, WATCHDOG_OS_SHUTDOWN, mode_str,
        )

    def stop(self):
        self._running = False

    def set_serial(self, ser):
        with self._lock:
            self._ser = ser

    def data_received(self, data: dict):
        with self._lock:
            self._last_data_ts = time.time()
            self._silence_warn_count = 0  # reset compteur warnings silence

            gas = data.get("g", 0)
            if gas >= 450:
                self._gas_danger_n += 1
            else:
                self._gas_danger_n = 0

            err = data.get("err", 0)
            if (err & 0x03) == 0x03:
                self._sensors_dead_n += 1
            else:
                self._sensors_dead_n = 0

            if self._esd_sent:
                if data.get("pump", 1) == 0 and data.get("valve", 1) == 0:
                    log.info("Watchdog : ESD acquitté (pompe+vanne OFF)")
                    self._esd_sent = False

    def pipeline_ok(self):
        with self._lock:
            self._error_count = 0

    def pipeline_error(self):
        with self._lock:
            self._error_count += 1
            if self._error_count % 5 == 0:
                log.warning("Watchdog : %d erreurs pipeline consécutives", self._error_count)

    # ── Boucle de surveillance ────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            time.sleep(WATCHDOG_TICK_S)
            try:
                self._check()
            except Exception as e:
                log.error("Watchdog loop exception : %s", e)

    def _check(self):
        with self._lock:
            now = time.time()

            # ── 1. Silence Arduino ────────────────────────────────────────────
            silence = now - self._last_data_ts
            if silence > WATCHDOG_TIMEOUT_S:
                if IS_SIMULATED:
                    # Mode Codespaces / bridge PC :
                    # Le bridge peut être déconnecté sans que ce soit critique.
                    # On avertit toutes les 60s sans arrêter le système.
                    self._silence_warn_count += 1
                    if self._silence_warn_count == 1 or self._silence_warn_count % 30 == 0:
                        log.warning(
                            "Watchdog [BRIDGE] : aucune donnée Arduino depuis %.0fs. "
                            "Vérifier arduino_serial_bridge.py sur ton PC. "
                            "Système maintenu en ligne.",
                            silence,
                        )
                    # NE PAS appeler _trigger_shutdown en mode SIMULATED
                    return
                else:
                    # Mode série physique : arrêt réel
                    self._trigger_shutdown(
                        reason=(
                            f"SILENCE_ARDUINO : aucune donnée depuis {silence:.0f}s "
                            f"(seuil={WATCHDOG_TIMEOUT_S}s).\n"
                            f"  → Vérifier le câblage USB Arduino\n"
                            f"  → Port série : {SERIAL_PORT}"
                        ),
                        do_os_shutdown=True,
                    )
                    return

            # ── 2. Capteurs ultrasons HS ──────────────────────────────────────
            if self._sensors_dead_n >= WATCHDOG_SENSORS_MAX:
                self._trigger_shutdown(
                    reason=(
                        f"SENSORS_DEAD : HC-SR04 R1+R2 défaillants "
                        f"({self._sensors_dead_n} cycles). Arrêt préventif."
                    ),
                    do_os_shutdown=False,
                )
                return

            # ── 3. Gaz danger persistant ──────────────────────────────────────
            if self._gas_danger_n >= WATCHDOG_CONFIRM_GAS:
                self._trigger_shutdown(
                    reason=(
                        f"GAS_CONFIRMED : MQ-4 > 450 ADC pendant "
                        f"{self._gas_danger_n} mesures. Fuite méthane confirmée."
                    ),
                    do_os_shutdown=True,
                )
                return

            # ── 4. ESD non acquitté ───────────────────────────────────────────
            if self._esd_sent and (now - self._esd_sent_ts) > ESD_ACK_TIMEOUT_S:
                self._trigger_shutdown(
                    reason=(
                        f"ESD_UNACKNOWLEDGED : Arduino non réactif "
                        f"({ESD_ACK_TIMEOUT_S}s sans acquittement)."
                    ),
                    do_os_shutdown=True,
                )
                return

            # ── 5. Cascade d'erreurs pipeline ─────────────────────────────────
            if self._error_count >= WATCHDOG_MAX_ERRORS:
                self._trigger_shutdown(
                    reason=(
                        f"CASCADE_ERRORS : {self._error_count} erreurs "
                        f"consécutives dans le pipeline."
                    ),
                    do_os_shutdown=False,
                )
                return

    # ── Actions de shutdown ───────────────────────────────────────────────────

    def _trigger_shutdown(self, reason: str, do_os_shutdown: bool):
        log.critical("═══ WATCHDOG SHUTDOWN ═══ %s", reason)
        self._running = False
        self._send_esd_serial()
        self._publish_alert(reason)

        try:
            self._stop_main()
        except Exception as e:
            log.error("Watchdog : erreur stop_main : %s", e)

        if do_os_shutdown and WATCHDOG_OS_SHUTDOWN:
            self._os_halt()

    def _send_esd_serial(self):
        ser = self._ser
        if ser is None or not ser.is_open:
            log.warning("Watchdog : port série indisponible pour ESD direct")
            return
        for _ in range(3):
            try:
                ser.write(b"CMD:ESD\n")
                ser.flush()
                time.sleep(0.1)
            except Exception as e:
                log.error("Watchdog : erreur envoi ESD série : %s", e)
                break
        self._esd_sent    = True
        self._esd_sent_ts = time.time()
        log.critical("Watchdog : CMD:ESD envoyé sur port série (×3)")

    def _publish_alert(self, reason: str):
        if self._mqtt is None:
            return
        try:
            payload = json.dumps({
                "type":      "WATCHDOG_SHUTDOWN",
                "reason":    reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            self._mqtt._publish_raw("gnl/alerte", payload, qos=1, retain=True)
            log.info("Watchdog : alerte publiée sur gnl/alerte")
        except Exception as e:
            log.warning("Watchdog : publication MQTT échouée : %s", e)

    def _os_halt(self):
        log.critical("Watchdog : arrêt OS dans 5s (WATCHDOG_OS_SHUTDOWN=true)")
        time.sleep(5)
        try:
            subprocess.run(["sudo", "systemctl", "halt"], timeout=10, check=False)
        except Exception as e:
            log.error("Watchdog : arrêt OS échoué : %s", e)
            try:
                subprocess.run(["sudo", "halt"], timeout=10, check=False)
            except Exception:
                pass
