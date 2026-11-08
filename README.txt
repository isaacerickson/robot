ISAAC'S ROBOT - SETUP
=====================
You need: Raspberry Pi 5 running Raspberry Pi OS (64-bit, WITH desktop),
the robot hardware wired as before, a screen, and an xAI (Grok) API key
from https://console.x.ai

1. Copy the "robot" folder from the flash drive into your home folder
   on the Pi, so you have /home/<you>/robot
2. Open Terminal on the Pi and type:
       cd ~/robot
       bash install_on_pi.sh
   That installs the motor-board libraries, including
   Adafruit-Blinka-Raspberry-Pi-PWM (MotorKit will not import without
   it), and OpenCV's face-detection files: the opencv-data package plus
   a local copy of the official OpenCV 4.x Haar cascade.
3. Answer the questions (Wi-Fi name/password, Grok API key, and the
   update link if you have one - just press Enter to skip).
4. When it says Done, type:  sudo reboot

From then on the robot starts by itself every time you flip the power on.
The face fills the small 800x480 screen. If that window is minimized, it
comes back. Clicking desktop settings does not rebuild it. Esc, q, and
closing the window do not turn the robot off. Speech uses the USB speakers
(ALSA card "Device"), not HDMI. The listening mic is that same kind of
USB audio adapter (the 1/8 inch jack), not the camera microphone. Export
MIC_DEVICE to a piece of its name, or its device number, if the names
differ. The eyes look at a face and idle when no face is there. Walk up
and talk; it answers out loud.
Problems? Look at ~/robot/robot.log
Don't run robot_brain.py from Thonny anymore - the installer sets everything up.

The drive wheels stay off until talking, the face, and recognition look
right. In robot_brain.py, MOTORS_ENABLED is False. Set that to True (or
export MOTORS_ENABLED=1 before start) when it should drive again. The
neck can still turn to look. The wheels are stopped on startup.

PHONE PANEL
===========
On the same Wi-Fi, open this in the phone's browser:

    http://robot.local:8080

AUTOMATE leaves the wheels off and keeps listening for conversation.
The page lists lines the robot understood. CONTROL is hand driving:
hold Forward, Back, Left, or Right, and let go to stop. The camera
picture is the robot's webcam. Hear microphone plays the 1/8 inch jack
(the USB audio adapter, not the camera mic) and only works in CONTROL,
so it does not fight conversation listening. Type a line and press Say
to speak it on the USB speakers. The page does not show the API key.

REMOTE STATUS
=============
The Pi can upload a short status file so it can be read from GitHub without
logging into the Pi. Create a fine-grained personal access token with
Contents read and write on isaacerickson/robot only, then add this line to
~/robot/robot.env (that file is not committed):

    GH_LOG_TOKEN=github_pat_...

Then:  chmod 600 ~/robot/robot.env

About a minute after startup, and every 5 minutes after that, the Pi writes
status.txt on the separate "logs" branch. Uploads never change main, so
git pull on the Pi is unaffected. If GH_LOG_TOKEN is missing, the upload
is skipped.
