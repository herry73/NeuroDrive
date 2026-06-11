import socket
import json
import os

# Load config
config_path = os.path.join(os.path.dirname(__file__), "config.json")
with open(config_path) as f:
    config = json.load(f)

ESP32_IP = config["esp32_ip"]
ESP32_PORT = config["esp32_port"]

# Valid commands
COMMANDS = {
    "FORWARD": "F\n",
    "LEFT":    "L\n",
    "RIGHT":   "R\n",
    "STOP":    "S\n",
}

def send_command(command_name):
    """Send a single command string to the ESP32 via UDP."""
    if command_name not in COMMANDS:
        print(f"[ERROR] Unknown command: {command_name}")
        return

    packet = COMMANDS[command_name].encode("utf-8")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(packet, (ESP32_IP, ESP32_PORT))
        sock.close()
        print(f"[SENT] {command_name} → {ESP32_IP}:{ESP32_PORT}")
    except Exception as e:
        print(f"[ERROR] Failed to send {command_name}: {e}")