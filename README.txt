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
3. Answer the questions (Wi-Fi name/password, Grok API key, and the
   update link if you have one - just press Enter to skip).
4. When it says Done, type:  sudo reboot

From then on the robot starts by itself every time you flip the power on.
Press Esc on a keyboard to close the face. Problems? Look at ~/robot/robot.log
Don't run robot_brain.py from Thonny anymore - the installer sets everything up.

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
