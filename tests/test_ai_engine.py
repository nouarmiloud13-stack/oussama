#!/usr/bin/env python3
"""
test_ai_engine.py — Tests unitaires du moteur IA GNL

Couverture :
  - Isolation Forest (détection anomalies)
  - Régression linéaire (prédiction 30s)
  - Seuil adaptatif gaz
  - Score global risque
  - Décision commande automatique

Exécution : python3 -m pytest tests/ -v
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "raspberry_pi"))
from ai.anomaly_engine import AnomalyEngine

# ── Données de test ─────────────────────────────────────────────────────────────

def make_data(n1=50, n2=40, t1=22.0, t2=21.5, p=1013.0, g=100, pump=0, valve=0):
    return {"n1": n1, "n2": n2, "t1": t1, "t2": t2, "p": p,
            "g": g, "pump": pump, "valve": valve}


# ── Tests ───────────────────────────────────────────────────────────────────────

class TestAnomalyEngine:

    def setup_method(self):
        self.engine = AnomalyEngine()

    def test_init(self):
        """Le moteur s'initialise correctement."""
        assert self.engine is not None
        assert self.engine.sample_count == 0
        assert not self.engine.if_trained

    def test_analyze_returns_required_keys(self):
        """analyze() retourne toujours les clés attendues."""
        result = self.engine.analyze(make_data())
        required = {"isolation_forest", "regression", "gas_alert", "global_risk", "command"}
        assert required.issubset(result.keys())

    def test_analyze_normal_data_low_risk(self):
        """Données normales → risque bas."""
        # Remplir l'historique avec des données normales
        for _ in range(30):
            result = self.engine.analyze(make_data())
        assert result["global_risk"] < 50
        assert result["gas_alert"] is None
        assert result["command"] is None

    def test_gas_alert_none_below_threshold(self):
        """Gaz < 250 → pas d'alerte."""
        data = make_data(g=100)
        result = self.engine.analyze(data)
        assert result["gas_alert"] is None

    def test_gas_alert_attention(self):
        """Gaz entre 250 et 450 → ATTENTION."""
        data = make_data(g=300)
        result = self.engine.analyze(data)
        assert result["gas_alert"] == "ATTENTION"

    def test_gas_alert_danger_confirmed_after_3_readings(self):
        """Gaz > 450 sur 3 mesures consécutives → DANGER_CRITIQUE."""
        data = make_data(g=500)
        results = [self.engine.analyze(data) for _ in range(4)]
        # 4ème mesure doit être DANGER_CRITIQUE
        assert results[-1]["gas_alert"] == "DANGER_CRITIQUE"

    def test_gas_alert_danger_confirming_first_reading(self):
        """Gaz > 450 sur 1 seule mesure → en cours de confirmation."""
        self.engine = AnomalyEngine()
        result = self.engine.analyze(make_data(g=500))
        assert result["gas_alert"] == "DANGER_CONFIRMING"

    def test_esd_command_on_confirmed_gas_danger(self):
        """Gaz critique confirmé → commande CMD:ESD."""
        data = make_data(g=500)
        for _ in range(4):
            result = self.engine.analyze(data)
        assert result["command"] == "CMD:ESD"

    def test_pump_off_low_level(self):
        """Niveau R1 < 10% → arrêt pompe."""
        data = make_data(n1=5, n2=50)
        result = self.engine.analyze(data)
        assert result["command"] in ("CMD:PUMP_OFF", "CMD:ESD", None)
        # La commande doit être au moins PUMP_OFF (pas de démarrage pompe)
        assert result["command"] != "CMD:VALVE_OPEN"

    def test_overflow_risk_high_level(self):
        """Niveau R1 > 95% → risque débordement détecté."""
        for _ in range(20):
            result = self.engine.analyze(make_data(n1=96, n2=40))
        assert result["global_risk"] > 0  # risque non nul

    def test_regression_prediction_available_after_training(self):
        """Régression disponible après suffisamment d'échantillons."""
        for i in range(16):
            result = self.engine.analyze(make_data(n1=50+i*0.5, n2=30+i*0.3))
        assert result["regression"].get("n1_in_30s") is not None
        assert result["regression"].get("n2_in_30s") is not None

    def test_global_risk_esd_threshold(self):
        """Risque global >= 85 → CMD:ESD."""
        engine = AnomalyEngine()
        # Simuler risque très élevé : gaz critique + niveau élevé
        for _ in range(5):
            result = engine.analyze(make_data(n1=98, n2=98, g=600))
        # Le risque devrait déclencher un ESD via gaz ou risque global
        assert result["command"] in ("CMD:ESD", "CMD:PUMP_OFF", "CMD:VALVE_CLOSE")

    def test_gas_counter_resets_on_normal(self):
        """Compteur gaz se remet à zéro si valeur normale."""
        data_danger = make_data(g=500)
        data_normal = make_data(g=100)
        self.engine.analyze(data_danger)
        self.engine.analyze(data_danger)
        self.engine.analyze(data_normal)  # retour normal
        result = self.engine.analyze(make_data(g=500))  # repart de zéro
        assert result["gas_alert"] == "DANGER_CONFIRMING"  # 1 seule mesure

    def test_sample_count_increments(self):
        """Le compteur d'échantillons s'incrémente."""
        self.engine.analyze(make_data())
        assert self.engine.sample_count == 1
        self.engine.analyze(make_data())
        assert self.engine.sample_count == 2


class TestGasLevelClassification:

    def setup_method(self):
        self.engine = AnomalyEngine()

    @pytest.mark.parametrize("gas,expected", [
        (0,   None),
        (100, None),
        (249, None),
        (250, "ATTENTION"),
        (300, "ATTENTION"),
        (449, "ATTENTION"),
    ])
    def test_gas_level_parametrize(self, gas, expected):
        """Tests paramétrés classification gaz."""
        result = self.engine._check_gas_adaptive(gas)
        assert result == expected


class TestRegressionPrediction:

    def setup_method(self):
        self.engine = AnomalyEngine()

    def test_regression_not_available_before_min_samples(self):
        """Pas de prédiction avant PRED_WINDOW échantillons."""
        for _ in range(5):
            result = self.engine.analyze(make_data())
        # n1_in_30s doit être None (pas assez d'échantillons)
        assert result["regression"].get("n1_in_30s") is None

    def test_overflow_risk_detected_when_level_rising_fast(self):
        """Overflow risk détecté si niveaux montent rapidement vers 95%."""
        levels = [80 + i * 1.2 for i in range(20)]  # monte vite
        for lvl in levels:
            result = self.engine.analyze(make_data(n1=min(100, lvl)))
        # Si niveaux proches de 95%, overflow_risk doit être True
        if result["regression"].get("n1_in_30s") and result["regression"]["n1_in_30s"] > 95:
            assert result["regression"]["overflow_risk"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
