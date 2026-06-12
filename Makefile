# ==============================================================================
#  Nouarmiloud IoT Edge — Makefile
#  Usage : make help
#  Mode Codespaces : make start
#    → démarre MongoDB + InfluxDB (Docker) + Edge Node (Python)
#    → sur ton PC : python arduino_serial_bridge.py --port COM3
# ==============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: all help start start-codespaces start-rpi start-local stop restart status logs clean \
        fclean install install-python install-system install-docker \
        setup-security setup-influx setup-mqtt up down build rebuild \
        test test-verbose test-coverage \
        download-gemma4 start-gemma4 stop-gemma4 gemma4-status \
        ngrok ngrok-install ngrok-config ngrok-start ngrok-stop \
        backup restore \
        check-deps check-ports check-serial check-ngrok \
        update-passwords \
        api-status api-login api-data api-alerts api-diagnostic api-history \
        mqtt-listen mqtt-publish-test \
        influx-query influx-backup \
        mongo-status mongo-logs mongo-express-open \
        grafana-open dashboard-open ngrok-open \
        lint format \
        docker-clean docker-logs docker-ps \
        logs-edge logs-gemma4 logs-influx logs-mongo

# ── Couleurs ───────────────────────────────────────────────────────────────────
RED    := \033[0;31m
GREEN  := \033[0;32m
YELLOW := \033[1;33m
BLUE   := \033[0;34m
CYAN   := \033[0;36m
BOLD   := \033[1m
NC     := \033[0m

# ── Variables ──────────────────────────────────────────────────────────────────
PROJECT_DIR   := $(shell pwd)
DOCKER_DIR    := $(PROJECT_DIR)/docker
RPI_DIR       := $(PROJECT_DIR)/raspberry_pi
TESTS_DIR     := $(PROJECT_DIR)/tests

COMPOSE        := docker compose -f $(DOCKER_DIR)/docker-compose.yml
COMPOSE_AI     := $(COMPOSE) --profile ai

PYTHON := python3
PIP    := pip3

-include .env
export

# ── Adresses ───────────────────────────────────────────────────────────────────
NGROK_DOMAIN    ?= theology-custody-rocky.ngrok-free.dev
NGROK_URL       ?= https://$(NGROK_DOMAIN)
API_URL         := $(NGROK_URL)/api/v1
LOCAL_GRAFANA   := http://localhost:3000
LOCAL_INFLUX    := http://localhost:8086
LOCAL_MONGO_UI  := http://localhost:8081
NGROK_DASH      := http://localhost:4040
MQTT_PORT       ?= 1883

# Credentials MongoDB (valeurs par défaut, surchargées par .env)
MONGO_USER      ?= gnl_admin
MONGO_PASS      ?= GNL_Mongo_2025!
MONGO_DB        ?= gnl_history
MONGO_TTL_DAYS  ?= 30

JWT_TOKEN := $(shell curl -s -X POST $(API_URL)/auth/login \
               -H "Content-Type: application/json" \
               -H "ngrok-skip-browser-warning: 1" \
               -d '{"username":"admin","password":"admin_GNL_2025!"}' \
               2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null)

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  COMMANDE PRINCIPALE
# ── ══════════════════════════════════════════════════════════════════════════ ──

## start            : Détecte l'environnement et lance le bon mode
start:
	@if [ -n "$$CODESPACE_NAME" ] || [ -n "$$CODESPACES" ]; then \
	  $(MAKE) start-codespaces --no-print-directory; \
	else \
	  $(MAKE) start-local --no-print-directory; \
	fi

## start-codespaces : Codespaces — Arduino bridge PC + MongoDB + InfluxDB (Docker) + Edge Node
start-codespaces: .env
	@echo ""
	@echo -e "$(BOLD)$(CYAN)╔══════════════════════════════════════════════════════════╗$(NC)"
	@echo -e "$(BOLD)$(CYAN)║   GNL IoT Edge — GitHub Codespaces (Données Réelles)     ║$(NC)"
	@echo -e "$(BOLD)$(CYAN)║   Arduino → Bridge PC → MQTT Public → Edge Node          ║$(NC)"
	@echo -e "$(BOLD)$(CYAN)╚══════════════════════════════════════════════════════════╝$(NC)"
	@echo ""
	@echo -e "$(GREEN)► Étape 1/5 — Installation dépendances Python...$(NC)"
	@$(PIP) install --quiet --break-system-packages \
	  -r $(RPI_DIR)/requirements.txt 2>/dev/null || \
	  pip install --quiet -r $(RPI_DIR)/requirements.txt 2>/dev/null || true
	@echo -e "$(GREEN)  ✓ Dépendances Python OK$(NC)"
	@echo ""
	@echo -e "$(GREEN)► Étape 2/5 — Démarrage InfluxDB (Docker)...$(NC)"
	@if docker info > /dev/null 2>&1; then \
	  $(COMPOSE) up -d influxdb 2>/dev/null \
	    && echo -e "$(GREEN)  ✓ InfluxDB démarré (port 8086)$(NC)" \
	    || echo -e "$(YELLOW)  ⚠ InfluxDB non démarré (ignoré)$(NC)"; \
	else \
	  echo -e "$(YELLOW)  ⚠ Docker non disponible — InfluxDB désactivé$(NC)"; \
	fi
	@echo ""
	@echo -e "$(GREEN)► Étape 3/5 — Démarrage MongoDB + Mongo-Express (Docker)...$(NC)"
	@if docker info > /dev/null 2>&1; then \
	  $(COMPOSE) up -d mongodb 2>/dev/null \
	    && echo -e "$(GREEN)  ✓ MongoDB démarré (port 27017)$(NC)" \
	    || echo -e "$(YELLOW)  ⚠ MongoDB non démarré — historique dashboard désactivé$(NC)"; \
	  echo -e "$(YELLOW)  Attente MongoDB prêt (max 30s)...$(NC)"; \
	  for i in 1 2 3 4 5 6; do \
	    sleep 5 && \
	    docker exec gnl_mongodb mongosh --eval "db.adminCommand('ping')" \
	      --quiet > /dev/null 2>&1 \
	      && echo -e "$(GREEN)  ✓ MongoDB prêt$(NC)" && break \
	      || echo -n "  ."; \
	  done; \
	  $(COMPOSE) up -d mongo_express 2>/dev/null \
	    && echo -e "$(GREEN)  ✓ Mongo-Express démarré (port 8081)$(NC)" \
	    || echo -e "$(YELLOW)  ⚠ Mongo-Express non démarré$(NC)"; \
	else \
	  echo -e "$(YELLOW)  ⚠ Docker non disponible — MongoDB désactivé$(NC)"; \
	fi
	@echo ""
	@echo -e "$(GREEN)► Étape 4/5 — Gemma4 (téléchargement si absent + démarrage Docker)...$(NC)"
	@if docker info > /dev/null 2>&1; then \
	  if [ ! -f "$(DOCKER_DIR)/models/gemma4/$(GEMMA4_MODEL_FILE)" ]; then \
	    echo -e "$(YELLOW)  Modèle Gemma4 absent — téléchargement automatique (~3.5 GB)...$(NC)"; \
	    $(MAKE) download-gemma4 --no-print-directory || \
	      echo -e "$(RED)  ✗ Téléchargement échoué — vérifier HF_TOKEN dans .env$(NC)"; \
	  fi; \
	  if [ -f "$(DOCKER_DIR)/models/gemma4/$(GEMMA4_MODEL_FILE)" ]; then \
	    $(COMPOSE) --profile ai up -d gemma4 2>/dev/null \
	      && echo -e "$(GREEN)  ✓ Gemma4 démarré (chargement modèle ~60s)$(NC)" \
	      || echo -e "$(YELLOW)  ⚠ Gemma4 non démarré$(NC)"; \
	  else \
	    echo -e "$(YELLOW)  ⚠ Gemma4 ignoré — mode AnomalyEngine seul$(NC)"; \
	  fi; \
	else \
	  echo -e "$(YELLOW)  ⚠ Docker non disponible — mode AnomalyEngine seul$(NC)"; \
	fi
	@echo ""
	@echo -e "$(GREEN)► Étape 5/5 — Démarrage Edge Node...$(NC)"
	@echo ""
	@echo -e "$(BOLD)━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(NC)"
	@echo -e "$(BOLD)  MQTT broker  :$(NC) broker.hivemq.com:1883 (public)"
	@echo -e "$(BOLD)  IA           :$(NC) Gemma4 local (Docker, localhost:8080)"
	@echo -e "$(BOLD)  API REST     :$(NC) http://0.0.0.0:5000"
	@echo -e "$(BOLD)  Dashboard    :$(NC) onglet Ports Codespaces → port 5000"
	@echo -e "$(BOLD)  MongoDB UI   :$(NC) http://localhost:8081  (admin / GNL_MongoUI_2025!)"
	@echo ""
	@echo -e "$(BOLD)$(YELLOW)  *** Sur ton PC (Arduino branché en USB) : ***$(NC)"
	@echo -e "$(YELLOW)    pip install pyserial paho-mqtt$(NC)"
	@echo -e "$(YELLOW)    python arduino_serial_bridge.py --port COM3$(NC)"
	@echo -e "$(YELLOW)    (Linux/Mac : --port /dev/ttyUSB0)$(NC)"
	@echo -e "$(BOLD)━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(NC)"
	@echo -e "  Ctrl+C pour arrêter"
	@echo ""
	@SERIAL_PORT=SIMULATED \
	 MQTT_HOST=broker.hivemq.com \
	 MQTT_PORT=1883 \
	 MQTT_PUBLIC=true \
	 API_HOST=0.0.0.0 \
	 API_PORT=5000 \
	 GNL_AI_PROVIDER=gemma4 \
	 GEMMA4_HOST=localhost \
	 GEMMA4_SERVER_PORT=8080 \
	 GEMMA4_TIMEOUT=60 \
	 GEMMA4_TEMPERATURE=0.15 \
	 INFLUX_URL=http://localhost:8086 \
	 INFLUX_TOKEN=gnl_influx_token_secret_2025 \
	 INFLUX_ORG=gnl_org \
	 INFLUX_BUCKET=gnl_monitoring \
	 MONGO_URI=mongodb://$(MONGO_USER):$(MONGO_PASS)@localhost:27017/ \
	 MONGO_DB=$(MONGO_DB) \
	 MONGO_TTL_DAYS=$(MONGO_TTL_DAYS) \
	 GNL_LOG_DIR=./logs \
	 PUBLIC_URL=http://localhost:5000 \
	 WATCHDOG_TIMEOUT_S=300 \
	 WATCHDOG_MAX_ERRORS=10 \
	 WATCHDOG_TICK_S=2.0 \
	 WATCHDOG_OS_SHUTDOWN=false \
	 CONFIRM_GAS=3 \
	 $(PYTHON) $(RPI_DIR)/gnl_main.py

## start-rpi        : Raspberry Pi physique (Gemma4 + Docker + ngrok)
start-rpi: check-deps .env
	@echo -e "$(BOLD)$(CYAN)╔══════════════════════════════════════════════════════════╗$(NC)"
	@echo -e "$(BOLD)$(CYAN)║   GNL IoT Edge — Raspberry Pi (Gemma4 + ngrok)           ║$(NC)"
	@echo -e "$(BOLD)$(CYAN)╚══════════════════════════════════════════════════════════╝$(NC)"
	@[ -n "$(NGROK_AUTHTOKEN)" ] && [ "$(NGROK_AUTHTOKEN)" != "CHANGE_ME" ] || \
	  (echo -e "$(RED)✗ NGROK_AUTHTOKEN manquant dans .env$(NC)" && exit 1)
	@echo -e "$(GREEN)► Étape 1/5 — Packages Python...$(NC)"
	@$(MAKE) install-python --no-print-directory
	@echo -e "$(GREEN)► Étape 2/5 — Modèle Gemma4...$(NC)"
	@if [ ! -f "$(DOCKER_DIR)/models/gemma4/$(GEMMA4_MODEL_FILE)" ]; then \
	  echo -e "$(YELLOW)  Modèle absent — make download-gemma4$(NC)"; \
	  $(MAKE) download-gemma4 --no-print-directory; \
	fi
	@echo -e "$(GREEN)► Étape 3/5 — ngrok...$(NC)"
	@$(MAKE) ngrok-install --no-print-directory
	@echo -e "$(GREEN)► Étape 4/5 — Images Docker...$(NC)"
	@$(MAKE) build --no-print-directory
	@echo -e "$(GREEN)► Étape 5/5 — Démarrage services...$(NC)"
	@$(COMPOSE) --profile ai up -d
	@sleep 30
	@$(MAKE) status --no-print-directory
	@echo -e "$(BOLD)$(GREEN)✅  GNL IoT Edge opérationnel !$(NC)"
	@echo -e "  Dashboard → $(NGROK_URL)"

## start-local      : Mode local sans ngrok (test rapide)
start-local: check-deps .env
	@echo -e "$(BOLD)$(CYAN)╔══════════════════════════════════════════╗$(NC)"
	@echo -e "$(BOLD)$(CYAN)║  GNL IoT Edge — Mode LOCAL               ║$(NC)"
	@echo -e "$(BOLD)$(CYAN)╚══════════════════════════════════════════╝$(NC)"
	@echo -e "$(GREEN)► Construction images Docker...$(NC)"
	@$(MAKE) build --no-print-directory
	@echo -e "$(GREEN)► Lancement services (Gemma4 + InfluxDB + MongoDB + Grafana)...$(NC)"
	@$(COMPOSE) --profile ai up -d mosquitto influxdb grafana gnl_edge mongodb mongo_express
	@sleep 30
	@$(MAKE) status --no-print-directory
	@echo -e "$(BOLD)$(GREEN)✅  GNL IoT démarré en mode LOCAL$(NC)"
	@echo -e "  API REST     → http://localhost:5000/api/v1"
	@echo -e "  Grafana      → http://localhost:3000"
	@echo -e "  InfluxDB     → http://localhost:8086"
	@echo -e "  MongoDB UI   → http://localhost:8081  (admin / GNL_MongoUI_2025!)"

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  CONTRÔLE DU SYSTÈME
# ── ══════════════════════════════════════════════════════════════════════════ ──

## stop             : Arrête tous les conteneurs Docker
stop:
	@echo -e "$(YELLOW)► Arrêt des services GNL...$(NC)"
	@$(COMPOSE) --profile ai down 2>/dev/null || \
	 $(COMPOSE) down 2>/dev/null || true
	@echo -e "$(GREEN)✓ Services arrêtés$(NC)"

## restart          : Redémarre
restart: stop
	@sleep 2
	@$(MAKE) up --no-print-directory

## up               : Lance sans rebuild
up:
	@$(COMPOSE) --profile ai up -d

## down             : Arrête et supprime
down:
	@$(COMPOSE) --profile ai down --remove-orphans

## build            : Construit les images Docker
build:
	@$(COMPOSE) build --no-cache

## rebuild          : Reconstruction forcée
rebuild:
	@$(COMPOSE) --profile ai down
	@$(COMPOSE) build --no-cache --pull
	@$(COMPOSE) --profile ai up -d

## status           : État de tous les services (API, Gemma4, InfluxDB, MongoDB, Grafana)
status:
	@echo -e "$(BOLD)$(BLUE)══ Conteneurs GNL ══$(NC)"
	@$(COMPOSE) ps 2>/dev/null || echo "(Docker non disponible)"
	@echo ""
	@echo -e "$(BOLD)$(BLUE)══ Santé des endpoints ══$(NC)"
	@echo -n "  API (local)   : " && \
	 curl -sf http://localhost:5000/health > /dev/null 2>&1 \
	 && echo -e "$(GREEN)● UP$(NC)" || echo -e "$(RED)● DOWN$(NC)"
	@echo -n "  Gemma4        : " && \
	 curl -sf http://localhost:8080/health > /dev/null 2>&1 \
	 && echo -e "$(GREEN)● UP$(NC)" \
	 || echo -e "$(YELLOW)● DOWN (make start-gemma4)$(NC)"
	@echo -n "  InfluxDB      : " && \
	 curl -sf http://localhost:8086/ping > /dev/null 2>&1 \
	 && echo -e "$(GREEN)● UP$(NC)" || echo -e "$(RED)● DOWN$(NC)"
	@echo -n "  MongoDB       : " && \
	 docker exec gnl_mongodb mongosh \
	   --eval "db.adminCommand('ping')" --quiet > /dev/null 2>&1 \
	 && echo -e "$(GREEN)● UP$(NC)" \
	 || echo -e "$(RED)● DOWN (make up ou make start-codespaces)$(NC)"
	@echo -n "  Mongo-Express : " && \
	 curl -sf http://localhost:8081 > /dev/null 2>&1 \
	 && echo -e "$(GREEN)● UP → http://localhost:8081$(NC)" \
	 || echo -e "$(YELLOW)● DOWN$(NC)"
	@echo -n "  Grafana       : " && \
	 curl -sf http://localhost:3000/api/health > /dev/null 2>&1 \
	 && echo -e "$(GREEN)● UP$(NC)" || echo -e "$(RED)● DOWN$(NC)"

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  INSTALLATION
# ── ══════════════════════════════════════════════════════════════════════════ ──

## install          : Installation complète
install: install-system install-python
	@echo -e "$(GREEN)✓ Installation complète$(NC)"

## install-system   : Dépendances système (apt)
install-system:
	@which apt-get > /dev/null 2>&1 || exit 0
	@sudo apt-get update -qq
	@sudo apt-get install -y -qq \
	    python3-pip python3-venv python3-serial \
	    mosquitto mosquitto-clients \
	    git curl openssl ufw fail2ban \
	    libopenblas-dev libatlas-base-dev 2>/dev/null || true

## install-python   : Packages Python
install-python:
	@$(PIP) install --break-system-packages -q -r $(RPI_DIR)/requirements.txt

## install-docker   : Docker + Docker Compose
install-docker:
	@which docker > /dev/null 2>&1 \
	  && echo -e "$(YELLOW)  Docker déjà installé$(NC)" && exit 0 || true
	@curl -fsSL https://get.docker.com | sudo bash
	@sudo usermod -aG docker $$USER

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  GEMMA4 — IA LOCALE
# ── ══════════════════════════════════════════════════════════════════════════ ──

## download-gemma4  : Télécharge Gemma4 Q4_K_M (~3.5 GB)
download-gemma4:
	@echo -e "$(BOLD)$(BLUE)╔══════════════════════════════════════════════╗$(NC)"
	@echo -e "$(BOLD)$(BLUE)║   Téléchargement Gemma4 E2B Q4_K_M (~3.5GB) ║$(NC)"
	@echo -e "$(BOLD)$(BLUE)╚══════════════════════════════════════════════╝$(NC)"
	@mkdir -p $(DOCKER_DIR)/models/gemma4
	@MODEL_FILE="$(DOCKER_DIR)/models/gemma4/$(GEMMA4_MODEL_FILE)"; \
	 if [ -f "$$MODEL_FILE" ]; then \
	   echo -e "$(GREEN)✓ Modèle déjà présent$(NC)"; ls -lh "$$MODEL_FILE"; exit 0; \
	 fi; \
	 $(PIP) install --break-system-packages -q "huggingface_hub[cli]>=0.23" 2>/dev/null || true; \
	 $(PYTHON) scripts/download_gemma4.py \
	   "$(DOCKER_DIR)/models/gemma4" \
	   "$(GEMMA4_MODEL_FILE)" \
	   "$(GEMMA4_MMPROJ_FILE)" || \
	 (echo -e "$(RED)✗ Téléchargement échoué$(NC)" && exit 1)
	@echo -e "$(GREEN)✓ Modèle Gemma4 téléchargé$(NC)"
	@ls -lh $(DOCKER_DIR)/models/gemma4/

## start-gemma4     : Lance uniquement Gemma4
start-gemma4:
	@[ -f "$(DOCKER_DIR)/models/gemma4/$(GEMMA4_MODEL_FILE)" ] || \
	 (echo -e "$(RED)✗ Modèle absent — make download-gemma4$(NC)" && exit 1)
	@echo -e "$(BLUE)► Démarrage Gemma4...$(NC)"
	@$(COMPOSE) --profile ai up -d gemma4
	@echo -e "$(YELLOW)  Chargement modèle (~60s)...$(NC)"
	@for i in 1 2 3 4 5 6 7 8 9; do \
	  sleep 10 && echo -n "  $${i}0s " && \
	  curl -sf http://localhost:8080/health > /dev/null 2>&1 \
	    && echo -e "→ $(GREEN)PRÊT$(NC)" && break || echo "..."; \
	done

## stop-gemma4      : Arrête Gemma4
stop-gemma4:
	@$(COMPOSE) --profile ai stop gemma4 2>/dev/null || \
	 docker stop gnl_gemma4 2>/dev/null || true
	@echo -e "$(GREEN)✓ Gemma4 arrêté$(NC)"

## gemma4-status    : État de Gemma4
gemma4-status:
	@echo -e "$(BOLD)$(BLUE)══ État Gemma4 (localhost:8080) ══$(NC)"
	@if curl -sf http://localhost:8080/health > /dev/null 2>&1; then \
	  echo -e "  $(GREEN)● Gemma4 : OPÉRATIONNEL$(NC)"; \
	  echo -e "  $(BLUE)  Endpoint : http://localhost:8080/completion$(NC)"; \
	else \
	  echo -e "  $(RED)● Gemma4 : HORS LIGNE$(NC)"; \
	  echo -e "  $(YELLOW)  → make start-gemma4$(NC)"; \
	  echo -e "  $(YELLOW)  → make download-gemma4  (si modèle absent)$(NC)"; \
	fi

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  MONGODB — HISTORIQUE DASHBOARD
# ── ══════════════════════════════════════════════════════════════════════════ ──

## mongo-status     : État de MongoDB et Mongo-Express
mongo-status:
	@echo -e "$(BOLD)$(BLUE)══ État MongoDB ══$(NC)"
	@if docker exec gnl_mongodb mongosh \
	    --eval "db.adminCommand('ping')" --quiet > /dev/null 2>&1; then \
	  echo -e "  $(GREEN)● MongoDB       : OPÉRATIONNEL (port 27017)$(NC)"; \
	  echo -e "  $(GREEN)● Mongo-Express : http://localhost:8081$(NC)"; \
	  echo -e "  $(BLUE)    Login : admin / GNL_MongoUI_2025!$(NC)"; \
	else \
	  echo -e "  $(RED)● MongoDB : HORS LIGNE$(NC)"; \
	  echo -e "  $(YELLOW)  → make up  ou  make start-codespaces$(NC)"; \
	fi

## mongo-logs       : Logs MongoDB (live)
mongo-logs:
	@$(COMPOSE) logs -f --tail=50 mongodb

## mongo-express-open : Ouvre l'UI MongoDB dans le navigateur
mongo-express-open:
	@xdg-open $(LOCAL_MONGO_UI) 2>/dev/null || \
	 open $(LOCAL_MONGO_UI) 2>/dev/null || \
	 echo -e "$(CYAN)→ $(LOCAL_MONGO_UI)  (admin / GNL_MongoUI_2025!)$(NC)"

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  NGROK — TUNNEL HTTPS
# ── ══════════════════════════════════════════════════════════════════════════ ──

## ngrok            : Installe, configure et lance ngrok
ngrok: ngrok-install ngrok-config ngrok-start

## ngrok-install    : Installe ngrok si absent
ngrok-install:
	@if which ngrok > /dev/null 2>&1; then \
	  echo -e "$(GREEN)  ✓ ngrok installé$(NC)"; \
	else \
	  ARCH=$$(uname -m); \
	  case "$$ARCH" in \
	    aarch64|arm64) PKG="linux-arm64" ;; \
	    armv7l|armv6l) PKG="linux-arm"   ;; \
	    *)              PKG="linux-amd64" ;; \
	  esac; \
	  curl -sSL "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-$$PKG.tgz" \
	    | sudo tar xz -C /usr/local/bin && \
	  echo -e "$(GREEN)  ✓ ngrok installé$(NC)"; \
	fi

## ngrok-config     : Configure le authtoken
ngrok-config:
	@[ -n "$(NGROK_AUTHTOKEN)" ] && [ "$(NGROK_AUTHTOKEN)" != "CHANGE_ME" ] || \
	  (echo -e "$(RED)  ✗ NGROK_AUTHTOKEN manquant dans .env$(NC)" && exit 1)
	@which ngrok > /dev/null 2>&1 && \
	  ngrok config add-authtoken "$(NGROK_AUTHTOKEN)" && \
	  echo -e "$(GREEN)  ✓ Authtoken configuré$(NC)" || true

## ngrok-start      : Lance le tunnel ngrok
ngrok-start:
	@echo -e "$(BLUE)► Lancement tunnel ngrok → $(NGROK_DOMAIN)...$(NC)"
	@which ngrok > /dev/null 2>&1 || (echo -e "$(RED)✗ ngrok absent$(NC)" && exit 1)
	@nohup ngrok http \
	  --domain="$(NGROK_DOMAIN)" \
	  --authtoken="$(NGROK_AUTHTOKEN)" \
	  5000 > /tmp/ngrok.log 2>&1 & \
	sleep 3; \
	curl -sf http://localhost:4040/api/tunnels > /dev/null 2>&1 \
	  && echo -e "$(GREEN)  ✓ Tunnel ngrok actif — https://$(NGROK_DOMAIN)$(NC)" \
	  || (echo -e "$(RED)  ✗ Tunnel non démarré$(NC)" && tail -10 /tmp/ngrok.log)

## ngrok-stop       : Arrête ngrok
ngrok-stop:
	@pkill -f "ngrok http" 2>/dev/null \
	  && echo -e "$(GREEN)✓ ngrok arrêté$(NC)" || true

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  MQTT
# ── ══════════════════════════════════════════════════════════════════════════ ──

## setup-mqtt       : Crée les utilisateurs Mosquitto
setup-mqtt:
	@docker exec gnl_mosquitto sh -c "\
	  mosquitto_passwd -c -b /mosquitto/data/passwd gnl_publisher 'GNL_Secure_2025!' && \
	  mosquitto_passwd    -b /mosquitto/data/passwd gnl_dashboard 'GNL_Dash_2025!'   && \
	  mosquitto_passwd    -b /mosquitto/data/passwd gnl_admin     'GNL_Admin_2025!'" \
	  2>/dev/null || true
	@docker exec gnl_mosquitto kill -HUP 1 2>/dev/null || true

## mqtt-listen      : Écoute tous les topics gnl/# (broker public HiveMQ)
mqtt-listen:
	@echo -e "$(BLUE)► Écoute MQTT broker.hivemq.com:1883 — Ctrl+C pour arrêter...$(NC)"
	@mosquitto_sub -h broker.hivemq.com -p 1883 -t "gnl/#" -v 2>/dev/null || \
	 docker run --rm eclipse-mosquitto:2.0 \
	   mosquitto_sub -h broker.hivemq.com -p 1883 -t "gnl/#" -v

## mqtt-publish-test : Publie un message de test MQTT
mqtt-publish-test:
	@echo -e "$(BLUE)► Publication message test MQTT (public broker)...$(NC)"
	@mosquitto_pub -h broker.hivemq.com -p 1883 \
	  -t "gnl/test" -m '{"test":true,"source":"makefile"}' \
	  && echo -e "$(GREEN)✓ Message publié$(NC)" || \
	 docker run --rm eclipse-mosquitto:2.0 \
	   mosquitto_pub -h broker.hivemq.com -p 1883 \
	   -t "gnl/test" -m '{"test":true,"source":"makefile"}'

## setup-influx     : Configure InfluxDB (première fois)
setup-influx:
	@sleep 3
	@docker exec gnl_influxdb influx setup \
	  --username gnl_admin --password "GNL_Influx_2025!" \
	  --org gnl_org --bucket gnl_monitoring --retention 30d --force \
	  2>/dev/null || echo -e "$(YELLOW)  InfluxDB déjà configuré$(NC)"

## influx-query     : Dernières mesures InfluxDB (5 dernières minutes)
influx-query:
	@curl -sf -XPOST "http://localhost:8086/api/v2/query" \
	  -H "Authorization: Token $(INFLUX_TOKEN)" \
	  -H "Content-Type: application/vnd.flux" \
	  -d 'from(bucket:"gnl_monitoring") |> range(start: -5m) |> last()' \
	  2>/dev/null | head -50 || echo -e "$(YELLOW)  InfluxDB non accessible$(NC)"

## influx-backup    : Sauvegarde InfluxDB vers backups/
influx-backup:
	@mkdir -p $(PROJECT_DIR)/backups
	@docker exec gnl_influxdb influx backup /tmp/influx_backup 2>/dev/null && \
	 docker cp gnl_influxdb:/tmp/influx_backup \
	   $(PROJECT_DIR)/backups/influx_$(shell date +%Y%m%d_%H%M%S)/ && \
	 echo -e "$(GREEN)✓ Sauvegarde OK$(NC)" || echo -e "$(RED)✗ Échec$(NC)"

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  API REST
# ── ══════════════════════════════════════════════════════════════════════════ ──

## api-status       : Health check API locale
api-status:
	@curl -sf -H "ngrok-skip-browser-warning: 1" \
	  http://localhost:5000/health | python3 -m json.tool 2>/dev/null || \
	 echo -e "$(RED)✗ API non accessible$(NC)"

## api-login        : Obtient un token JWT admin
api-login:
	@curl -s -X POST http://localhost:5000/api/v1/auth/login \
	  -H "Content-Type: application/json" \
	  -d '{"username":"admin","password":"admin_GNL_2025!"}' \
	  | python3 -m json.tool

## api-data         : Dernières mesures capteurs
api-data:
	@TOKEN=$$(curl -s -X POST http://localhost:5000/api/v1/auth/login \
	  -H "Content-Type: application/json" \
	  -d '{"username":"admin","password":"admin_GNL_2025!"}' \
	  | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))") && \
	curl -s http://localhost:5000/api/v1/data/latest \
	  -H "Authorization: Bearer $$TOKEN" | python3 -m json.tool

## api-diagnostic   : Diagnostic IA (Gemma4)
api-diagnostic:
	@TOKEN=$$(curl -s -X POST http://localhost:5000/api/v1/auth/login \
	  -H "Content-Type: application/json" \
	  -d '{"username":"admin","password":"admin_GNL_2025!"}' \
	  | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))") && \
	curl -s http://localhost:5000/api/v1/ai/diagnostic \
	  -H "Authorization: Bearer $$TOKEN" | python3 -m json.tool

## api-alerts       : Journal des alertes
api-alerts:
	@TOKEN=$$(curl -s -X POST http://localhost:5000/api/v1/auth/login \
	  -H "Content-Type: application/json" \
	  -d '{"username":"admin","password":"admin_GNL_2025!"}' \
	  | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))") && \
	curl -s http://localhost:5000/api/v1/alerts \
	  -H "Authorization: Bearer $$TOKEN" | python3 -m json.tool

## api-history      : Résumé historique du jour (MongoDB — teste les endpoints /history)
api-history:
	@TOKEN=$$(curl -s -X POST http://localhost:5000/api/v1/auth/login \
	  -H "Content-Type: application/json" \
	  -d '{"username":"admin","password":"admin_GNL_2025!"}' \
	  | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))") && \
	echo -e "$(BOLD)$(BLUE)── /history/summary ──$(NC)" && \
	curl -s http://localhost:5000/api/v1/history/summary \
	  -H "Authorization: Bearer $$TOKEN" | python3 -m json.tool && \
	echo -e "$(BOLD)$(BLUE)── /history/today (5 dernières) ──$(NC)" && \
	curl -s "http://localhost:5000/api/v1/history/today?limit=5" \
	  -H "Authorization: Bearer $$TOKEN" | python3 -m json.tool

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  LOGS
# ── ══════════════════════════════════════════════════════════════════════════ ──

## logs             : Logs de tous les services (live)
logs:
	@$(COMPOSE) --profile ai logs -f --tail=50

## logs-edge        : Logs Edge Node
logs-edge:
	@$(COMPOSE) logs -f --tail=100 gnl_edge

## logs-gemma4      : Logs Gemma4
logs-gemma4:
	@$(COMPOSE) --profile ai logs -f --tail=50 gemma4

## logs-influx      : Logs InfluxDB
logs-influx:
	@$(COMPOSE) logs -f --tail=50 influxdb

## logs-mongo       : Logs MongoDB
logs-mongo:
	@$(COMPOSE) logs -f --tail=50 mongodb

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  TESTS
# ── ══════════════════════════════════════════════════════════════════════════ ──

## test             : Tests unitaires (pytest)
test:
	@$(PIP) install --break-system-packages -q pytest pytest-cov 2>/dev/null || true
	@cd $(PROJECT_DIR) && $(PYTHON) -m pytest $(TESTS_DIR)/ -v --tb=short

## test-verbose     : Tests avec sortie détaillée
test-verbose:
	@cd $(PROJECT_DIR) && $(PYTHON) -m pytest $(TESTS_DIR)/ -vvv --tb=long -s

## test-coverage    : Tests avec rapport de couverture HTML
test-coverage:
	@$(PIP) install --break-system-packages -q pytest pytest-cov 2>/dev/null || true
	@cd $(PROJECT_DIR) && $(PYTHON) -m pytest $(TESTS_DIR)/ \
	  --cov=$(RPI_DIR)/ai --cov-report=html:coverage_html --cov-report=term-missing -v

## lint             : Analyse statique (flake8)
lint:
	@$(PIP) install --break-system-packages -q flake8 2>/dev/null || true
	@flake8 $(RPI_DIR) --max-line-length=100 --exclude=__pycache__ || true

## format           : Formatage automatique (black)
format:
	@$(PIP) install --break-system-packages -q black 2>/dev/null || true
	@black $(RPI_DIR) $(TESTS_DIR) --line-length=100

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  DIAGNOSTIC
# ── ══════════════════════════════════════════════════════════════════════════ ──

## check-deps       : Vérifie les dépendances (Python, Docker, Gemma4, MongoDB)
check-deps:
	@echo -e "$(BLUE)► Vérification dépendances...$(NC)"
	@echo -n "  python3   : " && which python3 > /dev/null 2>&1 \
	  && echo -e "$(GREEN)✓ ($(shell python3 --version 2>&1))$(NC)" \
	  || echo -e "$(RED)✗$(NC)"
	@echo -n "  pip3      : " && which pip3 > /dev/null 2>&1 \
	  && echo -e "$(GREEN)✓$(NC)" || echo -e "$(RED)✗$(NC)"
	@echo -n "  docker    : " && docker info > /dev/null 2>&1 \
	  && echo -e "$(GREEN)✓$(NC)" || echo -e "$(YELLOW)⚠ non disponible$(NC)"
	@echo -n "  Gemma4    : " && \
	  [ -f "$(DOCKER_DIR)/models/gemma4/$(GEMMA4_MODEL_FILE)" ] \
	  && echo -e "$(GREEN)✓ modèle présent$(NC)" \
	  || echo -e "$(YELLOW)⚠ absent (make download-gemma4)$(NC)"
	@echo -n "  MongoDB   : " && \
	  docker ps --filter "name=gnl_mongodb" --filter "status=running" -q \
	    2>/dev/null | grep -q . \
	  && echo -e "$(GREEN)✓ container running$(NC)" \
	  || echo -e "$(YELLOW)⚠ arrêté (make up ou make start-codespaces)$(NC)"

## check-ports      : Vérifie les ports requis (5000, 8080, 8086, 3000, 27017, 8081, 1883)
check-ports:
	@echo -e "$(BOLD)$(BLUE)══ Ports en écoute ══$(NC)"
	@for port in 5000 8080 8086 3000 27017 8081 1883 4040; do \
	  echo -n "  Port $$port : "; \
	  ss -tlnp 2>/dev/null | grep -q ":$$port " \
	    && echo -e "$(GREEN)● occupé$(NC)" || echo -e "$(YELLOW)○ libre$(NC)"; \
	done

## check-serial     : Ports série Arduino disponibles
check-serial:
	@ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || \
	  echo -e "$(YELLOW)  Aucun port série (normal en Codespaces — utiliser bridge PC)$(NC)"

## check-ngrok      : État du tunnel ngrok
check-ngrok:
	@curl -sf http://localhost:4040/api/tunnels | python3 -m json.tool 2>/dev/null || \
	 echo -e "$(RED)✗ ngrok non actif$(NC)"

## update-passwords : Change les mots de passe MQTT
update-passwords:
	@read -p "Mot de passe gnl_publisher : " p1 && \
	 docker exec gnl_mosquitto mosquitto_passwd \
	   -b /mosquitto/data/passwd gnl_publisher "$$p1"
	@read -p "Mot de passe gnl_dashboard : " p2 && \
	 docker exec gnl_mosquitto mosquitto_passwd \
	   -b /mosquitto/data/passwd gnl_dashboard "$$p2"
	@docker exec gnl_mosquitto kill -HUP 1 2>/dev/null || true

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  INTERFACES WEB
# ── ══════════════════════════════════════════════════════════════════════════ ──

## dashboard-open   : Ouvre le Dashboard HTML (port 5000)
dashboard-open:
	@xdg-open http://localhost:5000 2>/dev/null || \
	 open http://localhost:5000 2>/dev/null || \
	 echo -e "$(CYAN)→ http://localhost:5000$(NC)"

## grafana-open     : Ouvre Grafana (port 3000)
grafana-open:
	@xdg-open $(LOCAL_GRAFANA) 2>/dev/null || echo -e "$(CYAN)→ $(LOCAL_GRAFANA)$(NC)"

## ngrok-open       : Ouvre le dashboard ngrok (port 4040)
ngrok-open:
	@xdg-open $(NGROK_DASH) 2>/dev/null || echo -e "$(CYAN)→ $(NGROK_DASH)$(NC)"

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  NETTOYAGE
# ── ══════════════════════════════════════════════════════════════════════════ ──

## clean            : Supprime fichiers temporaires Python (__pycache__, .pyc)
clean:
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@rm -rf .pytest_cache coverage_html .coverage 2>/dev/null || true
	@echo -e "$(GREEN)✓ Nettoyage OK$(NC)"

## docker-clean     : Supprime ressources Docker GNL (volumes compris)
docker-clean:
	@read -p "Confirmer suppression Docker GNL (volumes MongoDB/InfluxDB inclus) [oui/NON] : " c \
	  && [ "$$c" = "oui" ] || exit 1
	@$(COMPOSE) --profile ai down -v --rmi local --remove-orphans
	@docker volume prune -f 2>/dev/null || true

## fclean           : Nettoyage complet (Python + Docker)
fclean: clean docker-clean

## docker-ps        : Liste les conteneurs GNL
docker-ps:
	@docker ps --filter "name=gnl_" \
	  --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

## docker-logs      : Logs récents des 3 conteneurs principaux
docker-logs:
	@docker logs --tail=30 gnl_edge_node 2>/dev/null || true
	@docker logs --tail=20 gnl_gemma4    2>/dev/null || true
	@docker logs --tail=20 gnl_mongodb   2>/dev/null || true

## backup           : Sauvegarde code + config dans backups/
backup:
	@mkdir -p $(PROJECT_DIR)/backups
	@BNAME="gnl_backup_$(shell date +%Y%m%d_%H%M%S)" && \
	 mkdir -p $(PROJECT_DIR)/backups/$$BNAME && \
	 cp -r $(RPI_DIR) $(PROJECT_DIR)/backups/$$BNAME/ && \
	 tar -czf $(PROJECT_DIR)/backups/$$BNAME.tar.gz \
	   -C $(PROJECT_DIR)/backups $$BNAME && \
	 rm -rf $(PROJECT_DIR)/backups/$$BNAME && \
	 echo -e "$(GREEN)✓ Sauvegarde : backups/$$BNAME.tar.gz$(NC)"

## restore          : Restaure une sauvegarde (voir backups/)
restore:
	@echo -e "$(YELLOW)Usage : tar -xzf backups/<fichier>.tar.gz -C .$(NC)"
	@ls -lh $(PROJECT_DIR)/backups/*.tar.gz 2>/dev/null || \
	  echo -e "$(YELLOW)  Aucune sauvegarde trouvée$(NC)"

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  GÉNÉRATION .env
# ── ══════════════════════════════════════════════════════════════════════════ ──

.env:
	@echo -e "$(YELLOW)► Fichier .env non trouvé — création avec valeurs par défaut...$(NC)"
	@printf '%s\n' \
	  '# GNL IoT Edge — Configuration' \
	  '# ⚠ Renseigner NGROK_AUTHTOKEN et HF_TOKEN avant de lancer' \
	  '' \
	  '# HuggingFace (téléchargement Gemma4)' \
	  'HF_TOKEN=hf_CHANGE_ME' \
	  '' \
	  '# ngrok (optionnel — accès externe)' \
	  'NGROK_AUTHTOKEN=CHANGE_ME' \
	  'NGROK_DOMAIN=theology-custody-rocky.ngrok-free.dev' \
	  'NGROK_URL=https://theology-custody-rocky.ngrok-free.dev' \
	  '' \
	  '# Arduino' \
	  'SERIAL_PORT=/dev/ttyUSB0' \
	  'SERIAL_BAUD=9600' \
	  '' \
	  '# Gemma4 — IA locale (llama.cpp)' \
	  'GEMMA4_VARIANT=e2b' \
	  'GEMMA4_QUANT=Q4_K_M' \
	  'GEMMA4_DEST=docker/models/gemma4' \
	  'GEMMA4_MODEL_FILE=google_gemma-4-e2b-it-Q4_K_M.gguf' \
	  'GEMMA4_MMPROJ_FILE=mmproj-google_gemma-4-e2b-it-bf16.gguf' \
	  'GEMMA4_HOST=localhost' \
	  'GEMMA4_SERVER_PORT=8080' \
	  'GEMMA4_CTX=4096' \
	  'GEMMA4_THREADS=4' \
	  'GEMMA4_GPU_LAYERS=0' \
	  'GEMMA4_TIMEOUT=60' \
	  'GEMMA4_TEMPERATURE=0.15' \
	  '' \
	  '# IA' \
	  'GNL_AI_PROVIDER=gemma4' \
	  'GNL_AI_INTERVAL=30' \
	  'GNL_AI_RISK_TRIGGER=60' \
	  'GNL_AI_MAX_TOKENS=512' \
	  '' \
	  '# Watchdog' \
	  'WATCHDOG_TIMEOUT_S=300' \
	  'WATCHDOG_MAX_ERRORS=10' \
	  'WATCHDOG_TICK_S=2.0' \
	  'WATCHDOG_OS_SHUTDOWN=false' \
	  'WATCHDOG_SENSORS_DEAD_MAX=5' \
	  'ESD_ACK_TIMEOUT_S=10' \
	  '' \
	  '# InfluxDB' \
	  'INFLUX_URL=http://localhost:8086' \
	  'INFLUX_TOKEN=gnl_influx_token_secret_2025' \
	  'INFLUX_ORG=gnl_org' \
	  'INFLUX_BUCKET=gnl_monitoring' \
	  'DOCKER_INFLUXDB_INIT_MODE=setup' \
	  'DOCKER_INFLUXDB_INIT_USERNAME=gnl_admin' \
	  'DOCKER_INFLUXDB_INIT_PASSWORD=GNL_Influx_2025!' \
	  'DOCKER_INFLUXDB_INIT_ORG=gnl_org' \
	  'DOCKER_INFLUXDB_INIT_BUCKET=gnl_monitoring' \
	  'DOCKER_INFLUXDB_INIT_RETENTION=30d' \
	  'DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=gnl_influx_token_secret_2025' \
	  '' \
	  '# MQTT' \
	  'MQTT_HOST=broker.hivemq.com' \
	  'MQTT_PORT=1883' \
	  'MQTT_USER_PUBLISHER=gnl_publisher' \
	  'MQTT_PASS_PUBLISHER=GNL_Secure_2025!' \
	  'MQTT_USER_DASHBOARD=gnl_dashboard' \
	  'MQTT_PASS_DASHBOARD=GNL_Dash_2025!' \
	  'MQTT_USER_ADMIN=gnl_admin' \
	  'MQTT_PASS_ADMIN=GNL_Admin_2025!' \
	  '' \
	  '# Grafana' \
	  'GF_SECURITY_ADMIN_USER=gnl_admin' \
	  'GF_SECURITY_ADMIN_PASSWORD=GNL_Grafana_2025!' \
	  'GF_USERS_ALLOW_SIGN_UP=false' \
	  '' \
	  '# API REST' \
	  'GNL_JWT_SECRET=gnl_jwt_secret_change_in_prod' \
	  'API_HOST=0.0.0.0' \
	  'API_PORT=5000' \
	  '' \
	  '# Seuils' \
	  'GAS_WARN=250' \
	  'GAS_DANGER=450' \
	  'LEVEL_HIGH=95' \
	  'LEVEL_LOW=10' \
	  'CONFIRM_GAS=3' \
	  '' \
	  '# MongoDB — Historique dashboard (TTL 30 jours)' \
	  'MONGO_USER=gnl_admin' \
	  'MONGO_PASS=GNL_Mongo_2025!' \
	  'MONGO_DB=gnl_history' \
	  'MONGO_TTL_DAYS=30' \
	  '# localhost:27017 pour mode Codespaces/local ; mongodb:27017 pour Docker interne' \
	  'MONGO_URI=mongodb://gnl_admin:GNL_Mongo_2025!@localhost:27017/' \
	  'MONGO_EXPRESS_USER=admin' \
	  'MONGO_EXPRESS_PASS=GNL_MongoUI_2025!' \
	  > .env
	@echo -e "$(GREEN)✓ .env créé$(NC)"
	@echo -e "$(YELLOW)  → Renseigner HF_TOKEN pour download-gemma4$(NC)"
	@echo -e "$(YELLOW)  → Renseigner NGROK_AUTHTOKEN pour accès externe$(NC)"

# ── ══════════════════════════════════════════════════════════════════════════ ──
##  AIDE
# ── ══════════════════════════════════════════════════════════════════════════ ──

## help             : Affiche cette aide
help:
	@echo ""
	@echo -e "$(BOLD)$(CYAN)╔══════════════════════════════════════════════════════════════╗$(NC)"
	@echo -e "$(BOLD)$(CYAN)║   Nouarmiloud IoT Edge — Makefile (M2 RSID 2025-2026)        ║$(NC)"
	@echo -e "$(BOLD)$(CYAN)╚══════════════════════════════════════════════════════════════╝$(NC)"
	@echo ""
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## //' | \
	  awk 'BEGIN{FS=":"} \
	    /^[A-Z]/ {printf "  $(BOLD)$(YELLOW)%-24s$(NC) %s\n", $$1, $$2; next} \
	    {printf "  $(CYAN)%-24s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo -e "$(BOLD)Flux de données (Codespaces) :$(NC)"
	@echo -e "  Arduino (PC USB) → arduino_serial_bridge.py → HiveMQ MQTT → Codespaces → Gemma4"
	@echo ""
	@echo -e "$(BOLD)Commandes essentielles :$(NC)"
	@echo -e "  $(GREEN)make start$(NC)               → Lance tout (détecte Codespaces/local automatiquement)"
	@echo -e "  $(GREEN)make download-gemma4$(NC)     → Télécharge Gemma4 (~3.5 GB)"
	@echo -e "  $(GREEN)make gemma4-status$(NC)       → Vérifie Gemma4"
	@echo -e "  $(GREEN)make mongo-status$(NC)        → Vérifie MongoDB + URL Mongo-Express"
	@echo -e "  $(GREEN)make mongo-express-open$(NC)  → Ouvre l'UI MongoDB (port 8081)"
	@echo -e "  $(GREEN)make api-history$(NC)         → Teste les endpoints /history (MongoDB)"
	@echo -e "  $(GREEN)make status$(NC)              → État de tous les services"
	@echo -e "  $(GREEN)make stop$(NC)                → Arrête tous les services"
	@echo ""

all: start
run: up
ps: docker-ps
