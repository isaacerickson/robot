#!/bin/bash
# One-time setup on the Raspberry Pi 5 (Raspberry Pi OS Bookworm, desktop version).
# Usage:  copy the robot folder to ~/robot, then:  cd ~/robot && bash install_on_pi.sh
set -e
ROBOT_DIR="$HOME/robot"
cd "$ROBOT_DIR"

echo "== Installing system packages =="
sudo apt update
sudo apt install -y python3-venv python3-opencv python3-lgpio python3-pygame \
    python3-pyaudio espeak-ng flac portaudio19-dev i2c-tools

echo "== Enabling I2C (motor HAT), desktop auto-login, no screen blanking =="
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_boot_behaviour B4   # boot to desktop, logged in automatically
sudo raspi-config nonint do_blanking 1          # keep the face screen on

echo "== Python environment =="
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip
pip install openai SpeechRecognition pyttsx3 adafruit-circuitpython-motorkit
pip install ultralytics || echo "ultralytics failed - robot will use the fallback detector"
python3 -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" 2>/dev/null || true  # pre-download model

echo "== Wi-Fi =="
read -p "Wi-Fi network name (leave blank to keep the current one): " SSID
if [ -n "$SSID" ]; then
  read -s -p "Wi-Fi password: " WIFIPASS; echo
  sudo nmcli dev wifi connect "$SSID" password "$WIFIPASS"
  sudo nmcli connection modify "$SSID" connection.autoconnect yes connection.autoconnect-priority 10
fi

echo "== Grok API key =="
if [ ! -f robot.env ]; then
  read -s -p "Paste your xAI API key (starts with xai-): " KEY; echo
  echo "XAI_API_KEY=$KEY" > robot.env
  chmod 600 robot.env
fi

echo "== Updates from chat =="
sudo apt install -y git
read -p "Update link (GitHub repo URL from Robot, or press Enter to skip): " REPO
if [ -n "$REPO" ]; then
  git init -q . && git remote remove origin 2>/dev/null; git remote add origin "$REPO"
  git fetch -q origin && git checkout -q -f -B main origin/main && git branch -q -u origin/main
  echo "Linked to $REPO - updates will install automatically."
fi
printf "venv/\nrobot.env\nrobot.log\n*.pt\n__pycache__/\n" > .git/info/exclude 2>/dev/null || true

echo "== Auto-start at power-on =="
chmod +x start_robot.sh
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/robot.desktop" << DESK
[Desktop Entry]
Type=Application
Name=Robot Brain
Exec=$ROBOT_DIR/start_robot.sh
X-GNOME-Autostart-enabled=true
DESK

echo
echo "Done. Reboot to test:  sudo reboot"
echo "Logs: $ROBOT_DIR/robot.log"
