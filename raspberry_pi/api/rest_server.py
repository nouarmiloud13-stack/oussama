#!/usr/bin/env python3
"""
rest_server.py — API REST Flask pour le système IoT GNL

Endpoints :
  GET  /api/v1/status          → état général du système
  GET  /api/v1/data/latest     → dernières mesures
  GET  /api/v1/data/history    → historique (paramètre : ?minutes=60)
  GET  /api/v1/ai/scores       → scores IA courants
  POST /api/v1/cmd/pompe       → commande pompe {action: "ON"|"OFF"}
  POST /api/v1/cmd/vanne       → commande vanne {action: "OPEN"|"CLOSE"}
  POST /api/v1/cmd/esd         → arrêt d'urgence
  GET  /api/v1/alerts          → journal alertes (30 dernières)
  GET  /health                 → health check systemd/Grafana

Sécurité : JWT Bearer token
CORS : configuré pour le domaine ngrok PUBLIC_URL
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from functools import wraps
from threading import Lock

from flask import Flask, jsonify, request, abort, send_from_directory
from flask_cors import CORS
import jwt

log = logging.getLogger("gnl.api")

# ── Config ─────────────────────────────────────────────────────────────────────
API_HOST   = os.environ.get("API_HOST", "0.0.0.0")
API_PORT   = int(os.environ.get("API_PORT", "5000"))
JWT_SECRET = os.environ.get("GNL_JWT_SECRET", "gnl_jwt_secret_change_in_prod")
JWT_ALGO   = "HS256"
JWT_EXPIRY = 3600  # 1 heure

# URL publique ngrok — utilisée pour les CORS
PUBLIC_URL = os.environ.get(
    "PUBLIC_URL", "https://theology-custody-rocky.ngrok-free.dev"
)

USERS = {
    "admin":    {"password": "admin_GNL_2025!", "role": "admin"},
    "operator": {"password": "oper_GNL_2025!",  "role": "operator"},
}

_DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard")

app   = Flask(__name__, static_folder=None)
_lock = Lock()


def _cors_origins():
    """Autorise: ngrok, localhost, et tous les domaines *.app.github.dev (Codespaces)."""
    return [
        PUBLIC_URL,
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        r"https://.*\.app\.github\.dev",
        r"https://.*\.preview\.app\.github\.dev",
    ]


CORS(app, resources={r"/*": {"origins": _cors_origins(), "supports_credentials": True}})

# État partagé
_latest_data:       dict = {}
_alerts:            list = []
_smart_diagnostic:  dict = {}
_mongo             = None   # MongoWriter injecté depuis gnl_main


def set_mongo(mongo_writer):
    global _mongo
    _mongo = mongo_writer  # dernier diagnostic Smart AI (Claude)

# ── Middleware ngrok — bypass page d'avertissement ─────────────────────────────
@app.before_request
def _ngrok_skip_warning():
    """
    ngrok affiche une page interstitielle 'Visit Site' sur les requêtes browser.
    L'en-tête 'ngrok-skip-browser-warning' avec n'importe quelle valeur
    (ex. '1') bypass cet écran côté client.
    Ce middleware l'ajoute automatiquement sur toutes les réponses de l'API,
    ce qui évite de le répéter dans chaque appel fetch() du dashboard.
    """
    pass  # L'en-tête est ajouté dans after_request


@app.after_request
def _add_ngrok_header(response):
    """Ajoute les en-têtes nécessaires pour ngrok + CORS + Codespaces."""
    response.headers["ngrok-skip-browser-warning"] = "1"
    # Autoriser dynamiquement l'origine de la requête (ngrok, Codespaces, localhost)
    origin = request.headers.get("Origin", "")
    allowed = (
        origin == PUBLIC_URL
        or origin.startswith("http://localhost")
        or origin.startswith("http://127.0.0.1")
        or origin.endswith(".app.github.dev")      # GitHub Codespaces
        or origin.endswith(".preview.app.github.dev")
    )
    if allowed and origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, ngrok-skip-browser-warning"
    )
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ── Helpers auth ────────────────────────────────────────────────────────────────

def generate_token(username: str, role: str) -> str:
    payload = {
        "sub":  username,
        "role": role,
        "iat":  int(time.time()),
        "exp":  int(time.time()) + JWT_EXPIRY,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def require_auth(role: str = "operator"):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Répondre immédiatement aux pré-vols OPTIONS (CORS)
            if request.method == "OPTIONS":
                return jsonify({}), 200
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                abort(401, "Token manquant")
            token = auth_header[7:]
            payload = decode_token(token)
            if payload is None:
                abort(401, "Token invalide ou expiré")
            if role == "admin" and payload.get("role") != "admin":
                abort(403, "Droits insuffisants (admin requis)")
            request.user = payload
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ── Endpoints publics ───────────────────────────────────────────────────────────

@app.route("/")
def serve_dashboard():
    """Sert le dashboard HTML — accessible directement depuis le browser."""
    return send_from_directory(os.path.abspath(_DASHBOARD_DIR), "gnl_dashboard.html")


@app.route("/health")
def health():
    return jsonify({
        "status":    "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "public_url": PUBLIC_URL,
    })


@app.route("/api/v1/auth/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    body     = request.get_json(silent=True) or {}
    username = body.get("username", "")
    password = body.get("password", "")
    user     = USERS.get(username)
    if not user or user["password"] != password:
        abort(401, "Identifiants incorrects")
    token = generate_token(username, user["role"])
    log.info("Connexion réussie : %s (role=%s)", username, user["role"])
    return jsonify({"token": token, "role": user["role"], "expires_in": JWT_EXPIRY})


# ── Endpoints protégés ─────────────────────────────────────────────────────────

@app.route("/api/v1/status")
@require_auth("operator")
def status():
    with _lock:
        data = dict(_latest_data)
    ai = data.get("ai", {})
    return jsonify({
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "node":        "rpi4_edge",
        "connected":   bool(data),
        "global_risk": ai.get("global_risk", 0),
        "gas_alert":   ai.get("gas_alert"),
        "pump":        data.get("pump", 0),
        "valve":       data.get("valve", 0),
        "public_url":  PUBLIC_URL,
    })


@app.route("/api/v1/data/latest")
@require_auth("operator")
def data_latest():
    with _lock:
        data = dict(_latest_data)
        diag = dict(_smart_diagnostic)
    if not data:
        return jsonify({"error": "Aucune donnée disponible"}), 503
    return jsonify({
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "niveau":     {"r1": data.get("n1"), "r2": data.get("n2")},
        "temperature":{"r1": data.get("t1"), "r2": data.get("t2")},
        "gaz":        {"adc": data.get("g"), "niveau": _gas_level(data.get("g", 0))},
        "pression":   data.get("p"),
        "actuateurs": {"pompe": data.get("pump"), "vanne": data.get("valve")},
        "ia":         data.get("ai", {}),
        "smart_ai":   diag,
    })


@app.route("/api/v1/ai/scores")
@require_auth("operator")
def ai_scores():
    with _lock:
        ai = _latest_data.get("ai", {})
    return jsonify({
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "isolation_forest": ai.get("isolation_forest", 0),
        "global_risk":      ai.get("global_risk", 0),
        "gas_alert":        ai.get("gas_alert"),
        "regression":       ai.get("regression", {}),
    })


@app.route("/api/v1/ai/diagnostic")
@require_auth("operator")
def ai_diagnostic():
    """Retourne le dernier diagnostic complet produit par le Smart AI (Claude)."""
    with _lock:
        diag = dict(_smart_diagnostic)
    if not diag:
        return jsonify({"error": "Aucun diagnostic disponible"}), 503
    return jsonify(diag)


@app.route("/api/v1/alerts")
@require_auth("operator")
def get_alerts():
    with _lock:
        alerts = list(reversed(_alerts[-30:]))
    return jsonify({"count": len(alerts), "alerts": alerts})


# ── Commandes (admin uniquement) ───────────────────────────────────────────────

@app.route("/api/v1/cmd/pompe", methods=["POST", "OPTIONS"])
@require_auth("admin")
def cmd_pompe():
    body   = request.get_json(silent=True) or {}
    action = body.get("action", "").upper()
    if action not in ("ON", "OFF"):
        abort(400, "action doit être ON ou OFF")
    _register_command(f"CMD:PUMP_{action}", request.user["sub"])
    return jsonify({"status": "queued", "command": f"CMD:PUMP_{action}"})


@app.route("/api/v1/cmd/vanne", methods=["POST", "OPTIONS"])
@require_auth("admin")
def cmd_vanne():
    body   = request.get_json(silent=True) or {}
    action = body.get("action", "").upper()
    if action not in ("OPEN", "CLOSE"):
        abort(400, "action doit être OPEN ou CLOSE")
    _register_command(f"CMD:VALVE_{action}", request.user["sub"])
    return jsonify({"status": "queued", "command": f"CMD:VALVE_{action}"})


@app.route("/api/v1/cmd/esd", methods=["POST", "OPTIONS"])
@require_auth("admin")
def cmd_esd():
    _register_command("CMD:ESD", request.user["sub"])
    log.critical("ESD déclenché via API par %s", request.user["sub"])
    return jsonify({"status": "queued", "command": "CMD:ESD"})


# ── File de commandes ──────────────────────────────────────────────────────────
_cmd_queue: list = []


def _register_command(cmd: str, user: str):
    with _lock:
        _cmd_queue.append({"cmd": cmd, "user": user, "ts": time.time()})
        _add_alert("COMMANDE_MANUELLE", cmd, user)


def pop_command() -> str | None:
    with _lock:
        if _cmd_queue:
            return _cmd_queue.pop(0)["cmd"]
    return None


def update_latest(data: dict):
    with _lock:
        _latest_data.clear()
        _latest_data.update(data)
        ai = data.get("ai", {})
        if ai.get("global_risk", 0) >= 70 or ai.get("gas_alert"):
            _add_alert(
                ai.get("gas_alert") or f"RISQUE_{ai.get('global_risk')}",
                data.get("g"),
                "auto_ia",
            )


def update_smart_diagnostic(diag: dict):
    """Met à jour le diagnostic Smart AI partagé avec l'API REST."""
    with _lock:
        _smart_diagnostic.clear()
        _smart_diagnostic.update(diag)
        severity = diag.get("severity", "INFO")
        if severity in ("DANGER", "CRITIQUE"):
            _add_alert(
                f"SMART_AI_{severity}",
                diag.get("diagnostic", ""),
                diag.get("source", "smart_ai"),
            )


def _add_alert(alert_type: str, value, source: str):
    _alerts.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type":   alert_type,
        "valeur": value,
        "source": source,
    })
    if len(_alerts) > 100:
        _alerts.pop(0)


def _gas_level(gas: int) -> str:
    if gas < 250:
        return "OK"
    if gas < 450:
        return "ATTENTION"
    return "DANGER"


# ── Historique MongoDB ─────────────────────────────────────────────────────────

@app.route("/api/v1/history/today")
@require_auth("operator")
def history_today():
    """Lectures capteurs du jour (dernières 120 entrées)."""
    if _mongo is None or not _mongo.available:
        return jsonify({"error": "MongoDB non disponible"}), 503
    limit = min(int(request.args.get("limit", 120)), 500)
    data  = _mongo.get_today_history(limit=limit)
    return jsonify({"count": len(data), "readings": data})


@app.route("/api/v1/history/diagnostics")
@require_auth("operator")
def history_diagnostics():
    """Diagnostics IA du jour (derniers 20)."""
    if _mongo is None or not _mongo.available:
        return jsonify({"error": "MongoDB non disponible"}), 503
    data = _mongo.get_today_diagnostics()
    return jsonify({"count": len(data), "diagnostics": data})


@app.route("/api/v1/history/events")
@require_auth("operator")
def history_events():
    """Évènements (alertes, ESD, commandes) du jour."""
    if _mongo is None or not _mongo.available:
        return jsonify({"error": "MongoDB non disponible"}), 503
    data = _mongo.get_today_events()
    return jsonify({"count": len(data), "events": data})


@app.route("/api/v1/history/summary")
@require_auth("operator")
def history_summary():
    """Résumé statistique du jour (min/max/moy + nb alertes)."""
    if _mongo is None or not _mongo.available:
        return jsonify({"error": "MongoDB non disponible"}), 503
    summary = _mongo.get_daily_summary()
    return jsonify(summary)


# ── Démarrage ──────────────────────────────────────────────────────────────────

def start_api_server():
    log.info("API REST démarrée sur %s:%d | URL publique : %s", API_HOST, API_PORT, PUBLIC_URL)
    app.run(host=API_HOST, port=API_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_api_server()
