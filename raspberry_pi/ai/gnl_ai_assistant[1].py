#!/usr/bin/env python3
"""
gnl_ai_assistant.py — Intégration Gemma4 dans le système IoT GNL
Raspberry Pi 4 / Docker — Edge AI via llama.cpp server

Rôle :
  - Interroge le serveur llama.cpp (Gemma4 E2B Q4_K_M) en local
  - Génère des diagnostics IA à partir des données capteurs
  - S'intègre dans la boucle principale gnl_main.py
  - Publie les analyses sur MQTT gnl/ia/gemma

Usage :
  from ai.gnl_ai_assistant import GemmaAssistant
  assistant = GemmaAssistant()
  diagnostic = assistant.analyze(data, ai_scores)
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("gnl.gemma")

# ── Configuration (depuis .env) ───────────────────────────────────────────────
GEMMA4_SERVER_PORT = os.environ.get("GEMMA4_SERVER_PORT", "8080")
GEMMA4_HOST        = os.environ.get("GEMMA4_HOST", "localhost")
GEMMA4_URL         = f"http://{GEMMA4_HOST}:{GEMMA4_SERVER_PORT}"
GEMMA4_CTX         = int(os.environ.get("GEMMA4_CTX", "4096"))

SYSTEM_PROMPT = os.environ.get(
    "GEMMA4_SYSTEM_PROMPT",
    "Tu es un assistant expert en surveillance de réservoirs GNL. "
    "Analyse les données capteurs et fournis des diagnostics de sécurité concis en français."
)

# Seuil : n'appelle Gemma que si risque > X% (économise les ressources)
GEMMA_RISK_THRESHOLD = int(os.environ.get("GEMMA4_RISK_THRESHOLD", "40"))

# Timeout requête (secondes) — Gemma4 E2B Q4_K_M sur RPi4 : ~3-8s/token
REQUEST_TIMEOUT = int(os.environ.get("GEMMA4_TIMEOUT", "30"))


class GemmaAssistant:
    """
    Client pour le serveur llama.cpp embarquant Gemma4 E2B.
    Génère des diagnostics IA contextualisés à partir des données GNL.
    """

    def __init__(self):
        self._available = self._check_server()
        if self._available:
            log.info("Gemma4 disponible sur %s", GEMMA4_URL)
        else:
            log.warning("Gemma4 non disponible — fonctionnement sans LLM")

    def _check_server(self) -> bool:
        """Vérifie si le serveur llama.cpp est accessible."""
        try:
            r = requests.get(f"{GEMMA4_URL}/health", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def analyze(self, sensor_data: dict, ai_scores: dict) -> dict | None:
        """
        Génère un diagnostic Gemma4 si le risque dépasse le seuil.

        Args:
            sensor_data : dict Arduino {"n1","n2","t1","t2","p","g","pump","valve"}
            ai_scores   : dict AnomalyEngine {"global_risk","isolation_forest",...}

        Returns:
            dict {
              "diagnostic": str,
              "severity": str,
              "actions": list[str],
              "timestamp": str
            } ou None si risque faible / Gemma indisponible
        """
        global_risk = ai_scores.get("global_risk", 0)

        # Ne pas solliciter Gemma si risque faible (économie CPU)
        if global_risk < GEMMA_RISK_THRESHOLD:
            return None

        if not self._available:
            self._available = self._check_server()
            if not self._available:
                return None

        prompt = self._build_prompt(sensor_data, ai_scores)

        try:
            response = requests.post(
                f"{GEMMA4_URL}/completion",
                json={
                    "prompt":      prompt,
                    "n_predict":   256,
                    "temperature": 0.2,   # réponses déterministes pour sécurité
                    "top_p":       0.9,
                    "stop":        ["</s>", "<end_of_turn>"],
                    "stream":      False,
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            text = response.json().get("content", "").strip()

            # Tenter de parser JSON si Gemma a répondu en JSON
            try:
                parsed = json.loads(text)
                parsed["timestamp"] = datetime.now(timezone.utc).isoformat()
                log.info(
                    "Gemma4 diagnostic (risque %d%%) : %s",
                    global_risk,
                    parsed.get("diagnostic", "")[:80]
                )
                return parsed
            except json.JSONDecodeError:
                # Retour texte brut si pas de JSON
                return {
                    "diagnostic": text,
                    "severity":   self._infer_severity(global_risk),
                    "actions":    [],
                    "timestamp":  datetime.now(timezone.utc).isoformat(),
                }

        except requests.Timeout:
            log.warning("Gemma4 timeout (%ds) — diagnostic ignoré", REQUEST_TIMEOUT)
            return None
        except Exception as e:
            log.warning("Gemma4 erreur : %s", e)
            return None

    def _build_prompt(self, data: dict, ai: dict) -> str:
        """Construit le prompt contextualisé avec les données capteurs."""
        reg = ai.get("regression", {})
        gas_alert = ai.get("gas_alert") or "Aucune"

        prompt = f"""<start_of_turn>user
{SYSTEM_PROMPT}

Données capteurs actuelles du système GNL :
- Réservoir R1 : {data.get('n1', '?')}% (prédit dans 30s : {reg.get('n1_in_30s', '?')}%)
- Réservoir R2 : {data.get('n2', '?')}% (prédit dans 30s : {reg.get('n2_in_30s', '?')}%)
- Température R1 : {data.get('t1', '?')}°C
- Température R2 : {data.get('t2', '?')}°C
- Pression : {data.get('p', '?')} hPa
- Capteur gaz MQ-4 : {data.get('g', '?')} (ADC 0-1023)
- Alerte gaz : {gas_alert}
- Pompe : {'EN MARCHE' if data.get('pump') else 'ARRÊTÉE'}
- Vanne : {'OUVERTE' if data.get('valve') else 'FERMÉE'}

Scores IA :
- Score risque global : {ai.get('global_risk', 0)}%
- Score Isolation Forest : {ai.get('isolation_forest', 0)}%
- Risque débordement prédit : {reg.get('overflow_risk', False)}

Réponds UNIQUEMENT en JSON avec ce format exact :
{{
  "diagnostic": "description courte du problème détecté",
  "severity": "INFO|ATTENTION|DANGER|CRITIQUE",
  "actions": ["action 1", "action 2"],
  "details": "explication technique en 1-2 phrases"
}}
<end_of_turn>
<start_of_turn>model
"""
        return prompt

    @staticmethod
    def _infer_severity(global_risk: int) -> str:
        if global_risk >= 85:
            return "CRITIQUE"
        if global_risk >= 70:
            return "DANGER"
        if global_risk >= 40:
            return "ATTENTION"
        return "INFO"
