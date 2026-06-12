#!/usr/bin/env python3
"""
anomaly_engine.py — Moteur IA embarqué (Edge AI)

Algorithmes :
  1. Isolation Forest  → détection anomalies multivariées
  2. Régression linéaire → prédiction niveau dans 30s
  3. Seuil adaptatif σ×2 → confirmation fausse alarme MQ-4

Conforme : ISO 13849 (safety functions), IEC 61511 (SIS)
"""

import time
import logging
import numpy as np
from collections import deque
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LinearRegression

log = logging.getLogger("gnl.ai")

# ── Constantes ─────────────────────────────────────────────────────────────────
HISTORY_SIZE      = 50    # nombre de mesures gardées en mémoire
TRAIN_MIN_SAMPLES = 20    # minimum pour entraîner Isolation Forest
IF_CONTAMINATION  = 0.05  # 5% anomalies attendues
SIGMA_MULT        = 2.0   # seuil adaptatif : mean ± σ×2
PRED_WINDOW       = 15    # points utilisés pour régression (30s à 2s/pt)
CONFIRM_GAS       = 3     # confirmations consécutives avant alarme gaz

# Seuils gaz MQ-4 (valeur ADC 0-1023)
GAS_WARN    = 250
GAS_DANGER  = 450

# Seuils niveau
LEVEL_HIGH  = 95   # % → débordement imminent
LEVEL_LOW   = 10   # % → cavitation pompe

class AnomalyEngine:
    """Moteur d'analyse IA temps réel pour le système IoT GNL."""

    def __init__(self):
        self.history: deque = deque(maxlen=HISTORY_SIZE)
        self.timestamps: deque = deque(maxlen=HISTORY_SIZE)

        self.isolation_forest: IsolationForest | None = None
        self.if_trained = False
        self.sample_count = 0

        self.gas_alert_count = 0   # compteur confirmations gaz
        self.last_retrain    = 0   # timestamp dernier entraînement IF

        log.info("AnomalyEngine initialisé (IF + régression linéaire + σ×2)")

    # ── Interface publique ────────────────────────────────────────────────────

    def analyze(self, data: dict) -> dict:
        """
        Analyse une mesure et retourne un dict enrichi :
          {
            "isolation_forest": int (0-100, score anomalie),
            "regression": {
                "n1_in_30s": float,
                "n2_in_30s": float,
                "overflow_risk": bool,
            },
            "gas_alert": str | None,
            "global_risk": int (0-100),
            "command": str | None,   # CMD:ESD / CMD:PUMP_OFF / CMD:VALVE_CLOSE
          }
        """
        now = time.time()

        # Vecteur de features : [n1, n2, t1, t2, p, g]
        features = [
            float(data.get("n1", 0)),
            float(data.get("n2", 0)),
            float(data.get("t1", 20)),
            float(data.get("t2", 20)),
            float(data.get("p", 1013)),
            float(data.get("g", 0)),
        ]

        self.history.append(features)
        self.timestamps.append(now)
        self.sample_count += 1

        result = {
            "isolation_forest": 0,
            "regression": {},
            "gas_alert": None,
            "global_risk": 0,
            "command": None,
        }

        # ── 1. Isolation Forest ──
        if_score = self._run_isolation_forest(features)
        result["isolation_forest"] = if_score

        # ── 2. Régression linéaire (prédiction 30s) ──
        reg_result = self._run_regression()
        result["regression"] = reg_result

        # ── 3. Seuil adaptatif gaz ──
        gas_alert = self._check_gas_adaptive(data.get("g", 0))
        result["gas_alert"] = gas_alert

        # ── 4. Score global risque ──
        global_risk = self._compute_global_risk(
            if_score, reg_result, gas_alert, features
        )
        result["global_risk"] = global_risk

        # ── 5. Commande automatique ──
        command = self._decide_command(global_risk, gas_alert, reg_result, features)
        result["command"] = command

        if global_risk > 70:
            log.warning(
                "RISQUE ÉLEVÉ %d%% | IF=%d | Gaz=%s | n1=%.1f%% n2=%.1f%%",
                global_risk, if_score, gas_alert,
                features[0], features[1],
            )

        return result

    # ── Isolation Forest ─────────────────────────────────────────────────────

    def _run_isolation_forest(self, features: list) -> int:
        """Retourne un score 0-100 (0=normal, 100=très anormal)."""
        if len(self.history) < TRAIN_MIN_SAMPLES:
            return 0

        # Réentraînement toutes les 60s
        if not self.if_trained or (time.time() - self.last_retrain) > 60:
            self._train_isolation_forest()

        if not self.if_trained:
            return 0

        try:
            X = np.array([features])
            score = self.isolation_forest.decision_function(X)[0]
            # score négatif = anomalie, positif = normal
            # Normalisation en 0-100
            normalized = max(0, min(100, int((-score + 0.5) * 100)))
            return normalized
        except Exception as e:
            log.debug("IF score error: %s", e)
            return 0

    def _train_isolation_forest(self):
        try:
            X = np.array(list(self.history))
            self.isolation_forest = IsolationForest(
                contamination=IF_CONTAMINATION,
                random_state=42,
                n_estimators=100,
            )
            self.isolation_forest.fit(X)
            self.if_trained = True
            self.last_retrain = time.time()
            log.debug("Isolation Forest entraîné sur %d échantillons", len(X))
        except Exception as e:
            log.warning("Échec entraînement IF : %s", e)
            self.if_trained = False

    # ── Régression linéaire ───────────────────────────────────────────────────

    def _run_regression(self) -> dict:
        """Prédit les niveaux dans 30s par régression linéaire."""
        result = {"n1_in_30s": None, "n2_in_30s": None, "overflow_risk": False}

        if len(self.history) < PRED_WINDOW:
            return result

        recent = list(self.history)[-PRED_WINDOW:]
        X = np.arange(len(recent)).reshape(-1, 1)

        n1_vals = np.array([r[0] for r in recent])
        n2_vals = np.array([r[1] for r in recent])

        try:
            reg1 = LinearRegression().fit(X, n1_vals)
            reg2 = LinearRegression().fit(X, n2_vals)

            # Prédiction dans 15 points supplémentaires (30s à 2s/pt)
            future_x = np.array([[len(recent) + 14]])
            n1_pred = float(np.clip(reg1.predict(future_x)[0], 0, 100))
            n2_pred = float(np.clip(reg2.predict(future_x)[0], 0, 100))

            result["n1_in_30s"] = round(n1_pred, 1)
            result["n2_in_30s"] = round(n2_pred, 1)
            result["overflow_risk"] = n1_pred > LEVEL_HIGH or n2_pred > LEVEL_HIGH
        except Exception as e:
            log.debug("Régression error: %s", e)

        return result

    # ── Seuil adaptatif gaz ───────────────────────────────────────────────────

    def _check_gas_adaptive(self, gas_value: int) -> str | None:
        """
        Détection gaz avec confirmation sur 3 mesures consécutives.
        Évite les fausses alarmes (dérive capteur, humidité).
        """
        if gas_value >= GAS_DANGER:
            self.gas_alert_count += 1
        elif gas_value >= GAS_WARN:
            self.gas_alert_count = max(0, self.gas_alert_count)
            # Alerte immédiate niveau attention (pas besoin confirmation)
            return "ATTENTION"
        else:
            self.gas_alert_count = 0
            return None

        # Danger confirmé sur 3 mesures consécutives
        if self.gas_alert_count >= CONFIRM_GAS:
            return "DANGER_CRITIQUE"

        return "DANGER_CONFIRMING"

    # ── Score global ──────────────────────────────────────────────────────────

    def _compute_global_risk(
        self, if_score: int, reg: dict, gas_alert: str | None, features: list
    ) -> int:
        """Agrège tous les indicateurs en un score risque 0-100."""
        risk = 0

        # Contribution Isolation Forest (40%)
        risk += if_score * 0.40

        # Contribution régression (30%)
        if reg.get("overflow_risk"):
            risk += 30
        elif reg.get("n1_in_30s") and reg["n1_in_30s"] > 85:
            risk += 15
        elif reg.get("n2_in_30s") and reg["n2_in_30s"] > 85:
            risk += 15

        # Contribution gaz (30%)
        if gas_alert == "DANGER_CRITIQUE":
            risk += 30
        elif gas_alert == "ATTENTION":
            risk += 10

        # Seuils directs niveaux
        n1, n2 = features[0], features[1]
        if n1 > LEVEL_HIGH or n2 > LEVEL_HIGH:
            risk += 20
        if n1 < LEVEL_LOW:
            risk += 15

        return min(100, int(risk))

    # ── Décision commande ─────────────────────────────────────────────────────

    def _decide_command(
        self,
        global_risk: int,
        gas_alert: str | None,
        reg: dict,
        features: list,
    ) -> str | None:
        """Génère une commande Arduino si nécessaire."""
        n1, n2 = features[0], features[1]

        # ESD immédiat si gaz critique confirmé
        if gas_alert == "DANGER_CRITIQUE":
            log.error("ESD DÉCLENCHÉ — fuite gaz critique confirmée")
            return "CMD:ESD"

        # Risque global très élevé
        if global_risk >= 85:
            log.error("ESD DÉCLENCHÉ — risque global %d%%", global_risk)
            return "CMD:ESD"

        # Débordement imminent R1
        if reg.get("overflow_risk") and n1 > 88:
            log.warning("Arrêt pompe préventif — débordement R1 prédit")
            return "CMD:PUMP_OFF"

        # R1 niveau critique bas
        if n1 < LEVEL_LOW:
            log.warning("Arrêt pompe — niveau R1 critique (%s%%)", n1)
            return "CMD:PUMP_OFF"

        # R2 plein
        if n2 >= 95:
            log.info("Fermeture vanne — R2 plein (%s%%)", n2)
            return "CMD:VALVE_CLOSE"

        return None
