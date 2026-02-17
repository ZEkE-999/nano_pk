__version__ = "1.1.1"

import os
import signal
import sys
import time
import json
import telnetlib
import logging
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt
from channel_map_with_aliases import channel_map

# ------------------ Konfiguration (über ENV mit Defaults) ------------------
HOST         = os.getenv("NANOPK_HOST", "192.168.2.147")
PORT         = int(os.getenv("NANOPK_PORT", "23"))
MQTT_BROKER  = os.getenv("MQTT_BROKER", "192.168.2.110")
MQTT_PORT    = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER    = os.getenv("MQTT_USER", "mqtt-user")
MQTT_PASS    = os.getenv("MQTT_PASS", "password")
MQTT_BASE    = os.getenv("MQTT_TOPIC_BASE", "nano_pk")
MQTT_STATUS  = f"{MQTT_BASE}/status"
CLIENT_ID    = os.getenv("MQTT_CLIENT_ID", "nano-pk-bridge")

QOS          = int(os.getenv("MQTT_QOS", "1"))  # 0/1
FLOAT_EPS    = float(os.getenv("FLOAT_EPS", "0.001"))

# NEW: control MQTT retain via .env
MQTT_RETAIN  = os.getenv("MQTT_RETAIN", "true").lower() in ("1", "true", "yes", "on")

# ------------------ Konstanten ------------------
ZK_STATUS_MAP = {
    0: "Unbekannt", 1: "Aus", 2: "Startvorbereitung", 3: "Kessel Start",
    4: "Zündüberwachung", 5: "Zündung", 6: "Übergang LB", 7: "Leistungsbrand",
    8: "Gluterhaltung", 9: "Warten auf EA", 10: "Entaschung", 11: "-",
    12: "Putzen"
}

# ------------------ Logging ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("nano-pk")

# ------------------ Helpers ------------------
def get_device_class(name: str, unit: str) -> Optional[str]:
    if unit == "°C":
        return "temperature"
    elif unit == "%":
        return "humidity" if ("Feuchte" in name or "Luftfeuchte" in name) else None
    elif unit == "bar":
        return "pressure"
    elif "Leistung" in name:
        return "power"
    return None

def almost_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < FLOAT_EPS
        except Exception:
            return False
    return a == b

def parse_pm_line(line: str):
    if not line or not line.startswith("pm"):
        return None
    parts = line.strip().split()
    if len(parts) < 2:
        return None
    values = []
    for v in parts[1:]:
        try:
            values.append(float(v) if "." in v else int(v))
        except ValueError:
            values.append(None)
    return values

# ------------------ MQTT ------------------
def mqtt_connect() -> Optional[mqtt.Client]:
    # Explicitly use Callback API v1 to avoid deprecation warning on paho-mqtt 2.x
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=CLIENT_ID, clean_session=True)

    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)

    # Last Will: offline
    client.will_set(MQTT_STATUS, "offline", qos=QOS, retain=True)

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
        log.info(f"[MQTT] Connected to {MQTT_BROKER}:{MQTT_PORT}")
        # Birth: online
        client.publish(MQTT_STATUS, "online", qos=QOS, retain=True)
        return client
    except Exception as e:
        log.error(f"[MQTT] Connection failed: {e}")
        return None

def send_discovery(mqtt_client: mqtt.Client):
    device = {
        "identifiers": ["nano_pk"],
        "manufacturer": "ETA",
        "model": "nanoPK Telnet",
        "name": "ETA nanoPK"
    }

    availability = [{
        "topic": MQTT_STATUS,
        "payload_available": "online",
        "payload_not_available": "offline"
    }]

    # Sensors
    for idx, entry in channel_map.items():
        mqtt_name = entry.get("mqtt_name", entry["alias"])
        label     = entry.get("label",     entry["alias"])
        unit      = entry.get("unit",      "")

        config_topic = f"homeassistant/sensor/nano_pk_{mqtt_name}/config"
        state_topic  = f"{MQTT_BASE}/{mqtt_name}"

        payload = {
            "name": label,
            "state_topic": state_topic,
            "unique_id": f"nano_pk_{mqtt_name}",
            "device": device,
            "availability": availability,
        }

        # Only for numeric sensors (idx != 0 is boiler status text)
        if idx != 0:
            payload["unit_of_measurement"] = unit
            payload["state_class"] = "measurement"
            dc = get_device_class(label, unit)
            if dc:
                payload["device_class"] = dc

        mqtt_client.publish(config_topic, json.dumps(payload), qos=QOS, retain=True)
        log.info(f"[MQTT] Discovery sent: {label}")

    # Connectivity binary sensor (still useful)
    config_topic = "homeassistant/binary_sensor/nano_pk_status/config"
    payload = {
        "name": "NanoPK Status",
        "state_topic": MQTT_STATUS,
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
        "unique_id": "nano_pk_status",
        "device": device,
        "availability": availability,
    }
    mqtt_client.publish(config_topic, json.dumps(payload), qos=QOS, retain=True)
    log.info("[MQTT] Discovery for connectivity sensor sent")

# ------------------ Telnet ------------------
def connect_telnet_with_backoff() -> telnetlib.Telnet:
    delay = 1
    while True:
        try:
            log.info(f"[Telnet] Connecting to {HOST}:{PORT} …")
            tn = telnetlib.Telnet(HOST, PORT, timeout=10)
            log.info("[Telnet] Connected.")
            return tn
        except Exception as e:
            log.warning(f"[Telnet] {e} – retry in {delay}s …")
            time.sleep(delay)
            delay = min(delay * 2, 30)

# ------------------ Main Loop ------------------
class Bridge:
    def __init__(self):
        self.mqtt: Optional[mqtt.Client] = None
        self.tn: Optional[telnetlib.Telnet] = None
        self.last_values: Dict[int, Any] = {}
        self.running = True

    def start(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        self.mqtt = mqtt_connect()
        if not self.mqtt:
            log.error("MQTT unavailable – exiting.")
            sys.exit(1)

        send_discovery(self.mqtt)
        self.tn = connect_telnet_with_backoff()

        while self.running:
            try:
                raw = self.tn.read_until(b"\n", timeout=5)
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                values = parse_pm_line(line)
                if not values:
                    continue

                for idx, val in enumerate(values):
                    if idx not in channel_map or val is None:
                        continue

                    entry = channel_map[idx]
                    mqtt_name = entry.get("mqtt_name", entry["alias"])
                    topic = f"{MQTT_BASE}/{mqtt_name}"

                    if idx == 0:
                        payload: Any = ZK_STATUS_MAP.get(val, ZK_STATUS_MAP[0])
                    else:
                        payload = val

                    last = self.last_values.get(idx)
                    if last is not None and almost_equal(last, payload):
                        continue

                    # NEW: retain can be switched via MQTT_RETAIN env var
                    self.mqtt.publish(topic, payload, qos=QOS, retain=MQTT_RETAIN)
                    self.last_values[idx] = payload

            except Exception as e:
                log.error(f"[Loop] {e} – reconnecting telnet …")
                try:
                    if self.tn:
                        self.tn.close()
                except Exception:
                    pass

                # Publish offline while reconnecting
                try:
                    self.mqtt.publish(MQTT_STATUS, "offline", qos=QOS, retain=True)
                except Exception:
                    pass

                time.sleep(2)
                self.tn = connect_telnet_with_backoff()

                try:
                    self.mqtt.publish(MQTT_STATUS, "online", qos=QOS, retain=True)
                except Exception:
                    pass

        self.cleanup()

    def stop(self, *_):
        log.info("Stop signal received, shutting down …")
        self.running = False

    def cleanup(self):
        try:
            if self.mqtt:
                self.mqtt.publish(MQTT_STATUS, "offline", qos=QOS, retain=True)
        except Exception:
            pass

        try:
            if self.tn:
                self.tn.close()
        except Exception:
            pass

        try:
            if self.mqtt:
                self.mqtt.loop_stop()
                self.mqtt.disconnect()
        except Exception:
            pass

        log.info("Cleanup done. Bye.")

def main():
    log.info(f"Starting ETA nanoPK MQTT Bridge v{__version__} (retain={MQTT_RETAIN})")
    Bridge().start()

if __name__ == "__main__":
    main()
