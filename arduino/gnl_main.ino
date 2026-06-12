/*
====================================================
  PROTOTYPE IoT GNL — Arduino Uno
  Projet fin d'études M2 RSID 2025-2026
====================================================

CÂBLAGE (hardware validé) :
  D3  → Relais K1   Pompe       (LOW = ON, HIGH = OFF)
  D8  → Relais K2   Electrovanne(LOW = OPEN, HIGH = CLOSE)
  D4  → DS18B20     Data        (+ résistance 4.7 kΩ vers 5V)
  D5  → Buzzer      PWM
  D6  → HC-SR04 R2  TRIG
  D7  → HC-SR04 R2  ECHO
  D9  → HC-SR04 R1  TRIG
  D10 → HC-SR04 R1  ECHO
  D11 → LED Verte   (+ 220 Ω)  NORMAL
  D12 → LED Jaune   (+ 220 Ω)  ALERTE
  D13 → LED Rouge   (+ 220 Ω)  DANGER
  A0  → MQ-4        AOUT
  A4  → SDA   LCD I2C + BMP280
  A5  → SCL   LCD I2C + BMP280

LOGIQUE DE DISTRIBUTION :
  R1 >= 80%  ->  Pompe ON   (transfère R1 vers R2)
  R1 <  20%  ->  Pompe OFF  (protection cavitation)
  R2 >= 95%  ->  Vanne OPEN (draine R2 vers extérieur)
  R2 <  70%  ->  Vanne CLOSE(retour niveau normal)
  R2 >= 95%  ->  Pompe OFF aussi (pas remplir R2 plein)

LED + BUZZER selon MQ-4 :
  MQ4 < 150  : LED verte  | silence
  150-299    : LED jaune  | bip intermittent (alarme moyenne)
  >= 300     : LED rouge  | buzzer continu (alarme danger)

FORMAT JSON vers Raspberry Pi (toutes les 2 s) :
  {"n1":82,"n2":34,"t1":22.3,"t2":-127,"p":1013.2,"g":145,"pump":0,"valve":0,"err":0}
  t2 = -127 (1 seul DS18B20)

Bitmask "err" :
  bit0 = HC-SR04 R1  bit1 = HC-SR04 R2  bit2 = DS18B20  bit4 = BMP280

Commandes recues depuis Raspberry Pi :
  CMD:PUMP_ON / CMD:PUMP_OFF
  CMD:VALVE_OPEN / CMD:VALVE_CLOSE
  CMD:ESD
  RISK:<0-100>  -> score risque IA affiché sur LCD
====================================================
*/

#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BMP280.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// ── Broches ───────────────────────────────────────────────────────────────────
#define TRIG1         9
#define ECHO1        10
#define TRIG2         6
#define ECHO2         7
#define BUZZER        5
#define MQ4_PIN      A0
#define RELAY_POMPE   3    // LOW = pompe ON
#define RELAY_VANNE   8    // LOW = vanne OPEN

#define LED_VERTE    11
#define LED_JAUNE    12
#define LED_ROUGE    13

#define ONE_WIRE_BUS  4    // DS18B20 unique

// ── Seuils gaz MQ-4 ──────────────────────────────────────────────────────────
#define MQ4_NORMAL  150
#define MQ4_ALERTE  300
#define MQ4_DANGER  450    // ESD local immédiat

// ── Configuration réservoirs ──────────────────────────────────────────────────
#define HAUTEUR_R1   13.0   // cm (mesurer capteur -> fond réservoir vide)
#define HAUTEUR_R2   13.0   // cm

// Seuils pompe (R1)
#define POMPE_ON    80    // % R1 trop plein -> pompe ON
#define POMPE_OFF   20    // % R1 trop vide  -> pompe OFF (protection)

// Seuils vanne (R2)
#define VANNE_OPEN  95    // % R2 trop plein -> vanne OPEN
#define VANNE_CLOSE 70    // % R2 normal     -> vanne CLOSE

// ── Intervalles non-bloquants (ms) ───────────────────────────────────────────
#define INTERVAL_SENSORS  500
#define INTERVAL_JSON    2000
#define INTERVAL_LCD     3000
#define BUZZ_ON_MS        120
#define BUZZ_OFF_MS       380

// ── Objets ───────────────────────────────────────────────────────────────────
LiquidCrystal_I2C lcd(0x27, 16, 2);

Adafruit_BMP280 bmp;
bool  bmpOk          = false;
float lastValidTemp  = 25.0;
float lastValidPress = 1013.0;

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature ds18b20(&oneWire);
bool dsOk = false;

// ── Variables d'état ─────────────────────────────────────────────────────────
float   niveau1   = 50.0;
float   niveau2   = 50.0;
float   rawDist1  = -1;
float   rawDist2  = -1;
float   tempDS    = -127.0;
float   tempBMP   = 25.0;
float   pressBMP  = 1013.0;
int     gasValue  = 0;
uint8_t sensorErrors = 0;

bool    pumpON   = false;
bool    valveON  = false;    // true = vanne ouverte

// ── Contrôle IA ───────────────────────────────────────────────────────────────
bool          aiOverride       = false;
unsigned long aiOverrideExpiry = 0;
int           aiRisk           = 0;

// ── Timers non-bloquants ─────────────────────────────────────────────────────
unsigned long lastSensors = 0;
unsigned long lastJson    = 0;
unsigned long lastLcd     = 0;
unsigned long lastBuzz    = 0;
bool          buzzState   = false;
int           lcdPage     = 0;

// =============================================================================
// LECTURE DISTANCE HC-SR04 — médiane de 5 lectures pour stabilité
// =============================================================================
float readDistanceSafe(int trig, int echo) {
  float buf[5];
  int   n = 0;

  for (int i = 0; i < 5; i++) {
    digitalWrite(trig, LOW);
    delayMicroseconds(2);
    digitalWrite(trig, HIGH);
    delayMicroseconds(10);
    digitalWrite(trig, LOW);

    long dur = pulseIn(echo, HIGH, 25000);   // timeout 25ms → max ~4m
    if (dur > 0) {
      float d = dur * 0.034f / 2.0f;
      if (d >= 0.5f && d < 40.0f) {          // 0.5 cm min, 40 cm max
        buf[n++] = d;
      }
    }
    delay(15);                               // 15 ms entre chaque pulse
  }

  if (n < 2) return -1.0f;                  // trop peu de lectures valides

  // tri à bulles pour trouver la médiane (5 éléments max → rapide)
  for (int i = 0; i < n - 1; i++)
    for (int j = i + 1; j < n; j++)
      if (buf[j] < buf[i]) { float t = buf[i]; buf[i] = buf[j]; buf[j] = t; }

  return buf[n / 2];                        // valeur centrale = médiane
}

float distToNiveau(float dist, float hauteur) {
  if (dist < 0) return -1.0f;
  return constrain(((hauteur - dist) / hauteur) * 100.0f, 0.0f, 100.0f);
}

// =============================================================================
// LECTURE BMP280
// =============================================================================
void readBMP280() {
  if (!bmpOk) { sensorErrors |= 0x10; return; }

  float tt = bmp.readTemperature();
  float pp = bmp.readPressure() / 100.0f;

  if (tt > -40 && tt < 80 && pp > 300 && pp < 1100) {
    lastValidTemp  = tt;
    lastValidPress = pp;
    tempBMP  = tt;
    pressBMP = pp;
  } else {
    sensorErrors |= 0x10;
    tempBMP  = lastValidTemp;
    pressBMP = lastValidPress;
  }
}

// =============================================================================
// LECTURE CAPTEURS
// =============================================================================
void readAllSensors() {
  sensorErrors = 0;

  rawDist1 = readDistanceSafe(TRIG1, ECHO1);
  if (rawDist1 < 0) { sensorErrors |= 0x01; }
  else              { niveau1 = distToNiveau(rawDist1, HAUTEUR_R1); }

  delay(60);  // anti-diaphonie entre les deux HC-SR04

  rawDist2 = readDistanceSafe(TRIG2, ECHO2);
  if (rawDist2 < 0) { sensorErrors |= 0x02; }
  else              { niveau2 = distToNiveau(rawDist2, HAUTEUR_R2); }

  if (dsOk) {
    ds18b20.requestTemperatures();
    float t = ds18b20.getTempCByIndex(0);
    if (t == -127.0f || t == 85.0f) { sensorErrors |= 0x04; }
    else                              { tempDS = t; }
  } else {
    sensorErrors |= 0x04;
  }

  readBMP280();
  gasValue = analogRead(MQ4_PIN);
}

// =============================================================================
// ACTIONNEURS
// =============================================================================
void setPump(bool on) {
  pumpON = on;
  digitalWrite(RELAY_POMPE, on ? LOW : HIGH);
}

void setValve(bool open) {
  valveON = open;
  digitalWrite(RELAY_VANNE, open ? LOW : HIGH);
}

void triggerESD() {
  setPump(false);
  setValve(false);
  aiOverride       = true;
  aiOverrideExpiry = millis() + 10000UL;
  digitalWrite(LED_VERTE, LOW);
  digitalWrite(LED_JAUNE, LOW);
  digitalWrite(LED_ROUGE, HIGH);
  for (int i = 0; i < 3; i++) {
    tone(BUZZER, 2800); delay(150);
    noTone(BUZZER);     delay(100);
  }
  tone(BUZZER, 2800);
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print(F("!! ESD URGENCE !!"));
  lcd.setCursor(0, 1); lcd.print(F("POMPE+VANNE OFF "));
}

// =============================================================================
// LEDs
// =============================================================================
void updateLeds() {
  if (gasValue >= MQ4_ALERTE) {
    digitalWrite(LED_VERTE, LOW); digitalWrite(LED_JAUNE, LOW); digitalWrite(LED_ROUGE, HIGH);
  } else if (gasValue >= MQ4_NORMAL) {
    digitalWrite(LED_VERTE, LOW); digitalWrite(LED_JAUNE, HIGH); digitalWrite(LED_ROUGE, LOW);
  } else {
    digitalWrite(LED_VERTE, HIGH); digitalWrite(LED_JAUNE, LOW); digitalWrite(LED_ROUGE, LOW);
  }
}

// =============================================================================
// BUZZER non-bloquant
// =============================================================================
void updateBuzzer(unsigned long now) {
  if (gasValue >= MQ4_ALERTE) {
    tone(BUZZER, 2000); buzzState = true;
  } else if (gasValue >= MQ4_NORMAL) {
    unsigned long p = buzzState ? (unsigned long)BUZZ_ON_MS : (unsigned long)BUZZ_OFF_MS;
    if (now - lastBuzz >= p) {
      lastBuzz  = now;
      buzzState = !buzzState;
      if (buzzState) tone(BUZZER, 800); else noTone(BUZZER);
    }
  } else {
    noTone(BUZZER); buzzState = false; lastBuzz = now;
  }
}

// =============================================================================
// DISTRIBUTION AUTOMATIQUE
//
//   R1 >= 80%  -> Pompe ON  (transfère R1 -> R2)
//   R1 <  20%  -> Pompe OFF (protection cavitation)
//   R2 >= 95%  -> Vanne OPEN (draine R2) + Pompe OFF
//   R2 <  70%  -> Vanne CLOSE
// =============================================================================
void autoDistribution() {
  if (aiOverride && millis() < aiOverrideExpiry) return;
  aiOverride = false;

  if (gasValue >= MQ4_DANGER) { setPump(false); setValve(false); return; }
  if (sensorErrors & 0x03)    { setPump(false); setValve(false); return; }

  // ── Électrovanne R2 ──────────────────────────────────────────────
  if (niveau2 >= VANNE_OPEN)        setValve(true);
  else if (niveau2 < VANNE_CLOSE)   setValve(false);

  // ── Pompe R1 -> R2 ───────────────────────────────────────────────
  // Ne pas pomper si R2 déjà plein (vanne ouverte = drain en cours)
  if (valveON || niveau2 >= VANNE_OPEN) {
    setPump(false);
  } else if (niveau1 >= POMPE_ON)  {
    setPump(true);
  } else if (niveau1 < POMPE_OFF)  {
    setPump(false);
  }
}

// =============================================================================
// LCD (2 pages, non-bloquant)
// =============================================================================
void updateLCD(unsigned long now) {
  if (now - lastLcd < INTERVAL_LCD) return;
  lastLcd = now;
  lcdPage = 1 - lcdPage;

  lcd.clear();

  if (gasValue >= MQ4_ALERTE) {
    lcd.setCursor(0, 0); lcd.print(F("!!! DANGER !!!  "));
    lcd.setCursor(0, 1);
    lcd.print(F("GAS:")); lcd.print(gasValue); lcd.print(F(" EVAC!  "));
    return;
  }

  if (lcdPage == 0) {
    // Page 1 : niveaux + actionneurs
    lcd.setCursor(0, 0);
    if (sensorErrors & 0x01) lcd.print(F("R1:ERR  "));
    else { lcd.print(F("R1:")); lcd.print((int)niveau1); lcd.print(F("% ")); }
    if (sensorErrors & 0x02) lcd.print(F("R2:ERR"));
    else { lcd.print(F("R2:")); lcd.print((int)niveau2); lcd.print(F("%")); }

    lcd.setCursor(0, 1);
    lcd.print(pumpON ? F("P:ON ") : F("P:OFF"));
    lcd.print(valveON ? F(" V:OPEN ") : F(" V:CLOSE"));
  } else {
    // Page 2 : température + pression + risque IA
    lcd.setCursor(0, 0);
    if (sensorErrors & 0x04) lcd.print(F("T:ERR       "));
    else { lcd.print(F("T:")); lcd.print(tempDS, 1); lcd.print(F("C ")); }
    if (aiRisk > 0) { lcd.print(F("R:")); lcd.print(aiRisk); lcd.print(F("%")); }

    lcd.setCursor(0, 1);
    if (sensorErrors & 0x10) lcd.print(F("P:ERR           "));
    else { lcd.print(F("P:")); lcd.print(pressBMP, 0); lcd.print(F("hPa ")); }
    if (gasValue >= MQ4_NORMAL) lcd.print(F("GAS!"));
  }
}

// =============================================================================
// JSON vers Raspberry Pi
// =============================================================================
void sendJSON(unsigned long now) {
  if (now - lastJson < INTERVAL_JSON) return;
  lastJson = now;

  Serial.print(F("{\"n1\":")); Serial.print((int)niveau1);
  Serial.print(F(",\"n2\":")); Serial.print((int)niveau2);
  Serial.print(F(",\"t1\":"));
  if (sensorErrors & 0x04) Serial.print(-127);
  else                       Serial.print(tempDS, 1);
  Serial.print(F(",\"t2\":")); Serial.print(-127);
  Serial.print(F(",\"p\":"));  Serial.print(pressBMP, 1);
  Serial.print(F(",\"g\":"));  Serial.print(gasValue);
  Serial.print(F(",\"pump\":")); Serial.print(pumpON  ? 1 : 0);
  Serial.print(F(",\"valve\":")); Serial.print(valveON ? 1 : 0);
  Serial.print(F(",\"err\":")); Serial.print(sensorErrors);
  Serial.println(F("}"));
}

// =============================================================================
// COMMANDES DEPUIS RASPBERRY PI
// =============================================================================
void readCommands() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();

  if      (cmd == F("CMD:PUMP_ON"))    { aiOverride = true; aiOverrideExpiry = millis() + 10000UL; setPump(true);   }
  else if (cmd == F("CMD:PUMP_OFF"))   { aiOverride = true; aiOverrideExpiry = millis() + 10000UL; setPump(false);  }
  else if (cmd == F("CMD:VALVE_OPEN")) { aiOverride = true; aiOverrideExpiry = millis() + 10000UL; setValve(true);  }
  else if (cmd == F("CMD:VALVE_CLOSE")){ aiOverride = true; aiOverrideExpiry = millis() + 10000UL; setValve(false); }
  else if (cmd == F("CMD:ESD"))        { triggerESD(); }
  else if (cmd.startsWith(F("RISK:"))) { aiRisk = cmd.substring(5).toInt(); }
}

// =============================================================================
// SETUP
// =============================================================================
void setup() {
  Serial.begin(9600);
  Wire.begin();

  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print(F("GNL STARTUP...  "));
  delay(800);

  pinMode(TRIG1,       OUTPUT); pinMode(ECHO1, INPUT);
  pinMode(TRIG2,       OUTPUT); pinMode(ECHO2, INPUT);
  pinMode(BUZZER,      OUTPUT);
  pinMode(RELAY_POMPE, OUTPUT); digitalWrite(RELAY_POMPE, HIGH); // pompe OFF
  pinMode(RELAY_VANNE, OUTPUT); digitalWrite(RELAY_VANNE, HIGH); // vanne CLOSE
  pinMode(LED_VERTE,   OUTPUT);
  pinMode(LED_JAUNE,   OUTPUT);
  pinMode(LED_ROUGE,   OUTPUT);

  ds18b20.begin();
  dsOk = (ds18b20.getDeviceCount() > 0);

  bmpOk = bmp.begin(0x76);
  if (!bmpOk) bmpOk = bmp.begin(0x77);

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(dsOk  ? F("DS:OK  ") : F("DS:ERR "));
  lcd.print(bmpOk ? F("BMP:OK") : F("BMP:ERR"));
  delay(1200);
  lcd.clear();

  digitalWrite(LED_VERTE, HIGH); delay(250); digitalWrite(LED_VERTE, LOW);
  digitalWrite(LED_JAUNE, HIGH); delay(250); digitalWrite(LED_JAUNE, LOW);
  digitalWrite(LED_ROUGE, HIGH); delay(250); digitalWrite(LED_ROUGE, LOW);
  tone(BUZZER, 1200, 150);

  lcd.setCursor(0, 0);
  lcd.print(F("Systeme pret !  "));
  delay(600);
  lcd.clear();

  Serial.println(F("=== GNL PRET ==="));
}

// =============================================================================
// LOOP — entièrement non-bloquant
// =============================================================================
void loop() {
  unsigned long now = millis();

  if (now - lastSensors >= INTERVAL_SENSORS) {
    lastSensors = now;
    readAllSensors();
    updateLeds();

    if (gasValue >= MQ4_DANGER) {
      setPump(false); setValve(false);
      aiOverride       = true;
      aiOverrideExpiry = millis() + 10000UL;
    } else {
      autoDistribution();
    }
  }

  updateBuzzer(now);
  updateLCD(now);
  sendJSON(now);
  readCommands();
}
