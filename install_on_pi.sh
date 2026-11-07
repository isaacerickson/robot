#!/bin/bash
# One-time setup on the Raspberry Pi 5 (Raspberry Pi OS 64-bit desktop:
# Bookworm or Trixie).
# Usage:  copy the robot folder to ~/robot, then:  cd ~/robot && bash install_on_pi.sh
set -e
ROBOT_DIR="$HOME/robot"
cd "$ROBOT_DIR"

echo "== Installing system packages =="
sudo apt update
# opencv-data supplies Haar cascades. Debian's python3-opencv has no cv2.data.
# curl is how start_robot.sh checks that https://api.x.ai is up.
# build-essential and python3-dev compile Adafruit-Blinka-Raspberry-Pi-PWM,
# which PyPI publishes as source only (no wheel).
sudo apt install -y python3-venv python3-opencv opencv-data python3-lgpio python3-pygame \
    python3-pyaudio espeak-ng flac portaudio19-dev i2c-tools curl \
    build-essential python3-dev

echo "== Enabling I2C (motor HAT), desktop auto-login, no screen blanking =="
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_boot_behaviour B4   # boot to desktop, logged in automatically
sudo raspi-config nonint do_blanking 1          # keep the face screen on

echo "== Python environment =="
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip
# MotorKit imports adafruit_blinka_raspberry_pi_pwm, which is a separate
# package (pip: Adafruit-Blinka-Raspberry-Pi-PWM).
# SpeechRecognition only captures the microphone (system PyAudio above).
# Transcription is Grok at https://api.x.ai/v1/stt using XAI_API_KEY.
# Do not pip-install whisper, torch, or ultralytics here. On a 2 GB Pi 5
# those have pulled huge CUDA wheels. Person detection uses OpenCV HOG +
# Haar and does not need YOLO. If ultralytics is already installed, the
# brain will use it; this script does not install it.
pip install openai SpeechRecognition pyttsx3 \
    adafruit-circuitpython-motorkit "Adafruit-Blinka-Raspberry-Pi-PWM>=1.0.2"
echo "Speech capture: PyAudio. Transcription and chat: xAI Grok (no local model)."
# This is the module import adafruit_motorkit pulls in on a Pi. Importing
# MotorKit here would also run Blinka's interactive dependency prompt.
python3 -c "import adafruit_blinka_raspberry_pi_pwm"
echo "MotorKit PWM backend import OK"

echo "== Haar cascade for the face fallback =="
# opencv-data puts this at /usr/share/opencv4/haarcascades/. Also keep the
# official OpenCV 4.x file next to robot_brain.py so detection still finds it
# if that package path differs.
HAAR_NAME="haarcascade_frontalface_default.xml"
HAAR_URL="https://raw.githubusercontent.com/opencv/opencv/4.10.0/data/haarcascades/${HAAR_NAME}"
if ! grep -q "<opencv_storage>" "$HAAR_NAME" 2>/dev/null; then
  rm -f "$HAAR_NAME"
  if curl -fsSL --max-time 60 -o "${HAAR_NAME}.partial" "$HAAR_URL" \
      && grep -q "<opencv_storage>" "${HAAR_NAME}.partial"; then
    mv "${HAAR_NAME}.partial" "$HAAR_NAME"
    echo "Saved ${HAAR_NAME}"
  else
    rm -f "${HAAR_NAME}.partial"
    echo "Could not download ${HAAR_NAME}; using opencv-data or HOG only."
  fi
fi

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
printf "venv/\nrobot.env\nrobot.log\n*.pt\n__pycache__/\nhaarcascade_frontalface_default.xml\n*.partial\n" > .git/info/exclude 2>/dev/null || true

echo "== USB speakers (ALSA card Device, not HDMI) =="
# Same route start_robot.sh applies on every launch. HDMI with nothing
# plugged in makes aplay exit 524. The Unitek adapter's card name is Device.
export ALSA_CARD=Device
cat > "$HOME/.asoundrc" << 'EOF'
pcm.!default {
    type plug
    slave.pcm "hw:Device,0"
}
ctl.!default {
    type hw
    card Device
}
EOF
echo "Default playback is plughw:Device,0"

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
