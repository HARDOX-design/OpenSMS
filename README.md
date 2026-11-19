<p align="center"><img width="20%" src="app/data/images/favicont512.png" /></p>

# 📨 OpenSMS
An open-source cross-platform solution to host a `SMS` API server locally.

<a href="https://github.com/daslearning-org/OpenSMS/releases" target="_blank" rel="noopener noreferrer"><img alt="GitHub Downloads (all assets, all releases)" src="https://img.shields.io/github/downloads/daslearning-org/OpenSMS/total"></a>

<details>
  <summary>Keeping things simple</summary>

  - It is meant to be used with a backend or application layer in the local network while `OpenSMS` serves as the SMS gateway / API.
  - To keep it simple, rate limiting or other restrictions may be applied at the application layer (not adding in `OpenSMS` app itself).
  - It uses the default `SIM` for sending sms as set in the android settings.
</details>


### 🤔 Why would you choose our app? (Features)
👉 Turns your `Android` phone into a SMS API server. <br>
👉 Forward received SMS to your own HTTP endpoint (webhook). <br>
👉 Also available on `Desktop` platforms via [EC200U](https://www.quectel.com/product/lte-ec200u-series/) or similar USB development boards. <br>
👉 Open-source, No advertisements and privacy focused.

## 💰 Sponsor Me
You can buy me a coffee via [this link](https://www.paypal.com/paypalme/soomnathsdas) or tap on below image. Thank you 🙏. <br>
<a href="https://www.paypal.com/paypalme/soomnathsdas"><img src="./docs/images/donate.svg" height="40"></a>

## 📽️ Demo
You can click on the below Image or this [Youtube Link](https://www.youtube.com/watch?v=wNXH_3onjfg) to see the demo. Please let me know in the comments, how do you feel about this App. <br>

<a href="https://www.youtube.com/watch?v=wNXH_3onjfg"><img src="docs/images/OpenSmsThumb.webp" width="70%"></a>

## 🧑‍💻 Quickstart Guide

### 📱 Android Platform
Minimum `Android 9` is required and our published app only support `64 bit`. (You may build for other architecture with `buildozer`)

1. You can check the [Releases](https://github.com/daslearning-org/OpenSMS/releases) and downlaod the latest version of the android app on your phone.
2. You may need to disable (temporarily) `Play Protect` from your `Play Store` settings.
3. Install the App & launch.
4. Grant the permissions.
5. Android might block `sms` permission. Go to `Settings` > `Apps` > `OpenSMS` > Tap on three vertical-dots at top-right corner > `Allow restricted access`.
6. Then grant `SMS` permission from the settings window.
7. Relaunch the app. 
8. Click `Start Server`. It will then prompt you to generate an API Key for the first time. Generate and store it securely.

### 🖥️ Desktop Platform
Prerequisites:

* We need a USB Development Board with SIM support, which supports `AT` commands via `Serial` port. Connect it to your desktop / server platform via USB. I am using [EC200U Board](https://sharvielectronics.com/product/esp32-s3-u-with-ec200u-4g-lte-cat-1-wifi-bluetooth-gnss-iot-smart-modem/) which also has ESP32-S3.

* Find your serial port which supports the AT commands. You can check the [driver installation](https://www.waveshare.com/wiki/EC200U-EU-C4-P01) section if you require more info.

* On Linux `Modem Manager` may automatically use the default serial port which is for `AT` commands. In that case you need to free the serial port.
```bash
sudo systemctl stop ModemManager # stop immediately, but auto starts on boot
sudo systemctl disable ModemManager # disbable it permanently
```

#### >_ CLI Mode (Recommended)
You can run any of the following modes. First clone the repo and install the modules.

- Download `openSmsCli` for Linux or `openSmsCli.exe` on Windows from the [Releases](https://github.com/daslearning-org/OpenSMS/releases). Then run below commands. You may run via Python as well.

```bash
# On Linux
chmod +x openSmsCli
./openSmsCli --help # get arguments detail
./openSmsCli interactive --serial /dev/ttyUSB0
```

```powershell
# On Windows
.\openSmsCli.exe --help
.\openSmsCli.exe interactive --serial COM1
```

- Or run it via Python
```bash
git clone https://github.com/daslearning-org/OpenSMS.git
cd OpenSMS/app
pip install -r req_cli.txt # virtual env is recommended
python openSmsCli.py --help # to know the arguments
```

- *Interactive Mode*:
You may need to run this first to generate the first API Key.
```bash
python openSmsCli.py interactive --serial /dev/ttyUSB0 # change your serial port, on windows: COM1 (example)

# generate a new API key
newkey

# get other interactive commands
help
```

- *Foreground Mode*:
```bash
python openSmsCli.py run --serial /dev/ttyUSB0
```

- *Background Mode*: 
It generates a background process with PID. You need to kill the process in order to stop it.
```bash
python openSmsCli.py start --serial /dev/ttyUSB0
```

#### UI Mode
We can also use the graphical app (same as android). Executable binaries will also be available in [Releases](https://github.com/daslearning-org/OpenSMS/releases) if you do not wish to avoid managing Python.

- Dedicated App based on OS: You will get `OpenSmsApp` for Linux and `OpenSmsApp.exe` for Windows. Just run it. You can also run it via Python as shown below.

- Run the UI via Python
```bash
git clone https://github.com/daslearning-org/OpenSMS.git
cd OpenSMS/app
pip install -r requirements.txt # virtual env is recommended
python main.py # Opens the app ui
```

- You can select the serial port (dropdown) & start the Serial Connection.

- Then you can start the API Server.

### 🧪 Test API endpoint
Find the network address of your device and you may test out the API. Sample given below (This sends sms to your given number).

```bash
curl -i -X POST 'http://127.0.0.1:9292/sms/' \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer my-secret-api-key' \
    -d '{"number": "+1234567890", "text": "Hello from OpenSMS API!"}'
```

## 📥 Receiving SMS (Android)

OpenSMS can forward every SMS received by the phone to your own HTTP endpoint as a webhook. Receiving works independently of the API server: it runs in the same background service, so the API can be stopped while webhooks keep flowing.

### Configure it

1. Open `Settings` and find the **Webhook (Inbound SMS)** section.
2. Enter the **Webhook URL** (`http://` or `https://`).
3. Optionally set a **Signing Secret** to sign every request.
4. Enable the switch and tap **Save**.
5. Tap **Send Test Webhook** to check the endpoint. This sends `event: "webhook.test"`.

Settings take effect immediately; no restart is needed. The last delivery result is shown under the webhook settings and in the `Logs` screen.

### Request format

Every Inbound SMS produces one `POST` with a JSON body:

```http
POST /your-hook HTTP/1.1
Content-Type: application/json
User-Agent: OpenSMS/1.1.0
X-OpenSMS-Event: sms.received
X-OpenSMS-Delivery: 6f1c2f5e-0d7a-4c55-9f0e-2b1c8b7f0a11
X-OpenSMS-Signature: sha256=...
```

```json
{
  "event": "sms.received",
  "delivery_id": "6f1c2f5e-0d7a-4c55-9f0e-2b1c8b7f0a11",
  "received_at": "2026-11-19T14:32:10+05:30",
  "sent_at": "2026-11-19T09:01:58+00:00",
  "from": "+14155552671",
  "text": "Hello from a real SMS",
  "parts": 1,
  "sim": {"subscription_id": 1, "slot": 0}
}
```

- `from` is the raw originating address. It can be an alphanumeric sender ID (for example `BANKALERT`), not only an E.164 number.
- `sent_at` is the SMS centre timestamp and may be `null`. `received_at` is when the phone received it.
- A multipart SMS is delivered as **one** webhook with the parts joined in `text` and counted in `parts`.
- `sim` is included only when the subscription can be determined.

A `2xx` response means delivered. Anything else (or a timeout after 10 seconds) is retried twice (after 5s and 30s); after three failed attempts the delivery is logged as failed. Pending webhooks are kept on disk and survive restarts. At most 100 are queued; when full, the oldest is dropped.

### Verify the signature

When a signing secret is set, `X-OpenSMS-Signature` is `sha256=` followed by the hex HMAC-SHA256 of the **raw request body**:

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(
    b"your-secret", raw_body, hashlib.sha256
).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-OpenSMS-Signature"])
```

### Things to know

- The URL must be reachable **from the phone**. For a LAN target, keep the phone on the same network.
- Receiving works while the background service is running (it survives closing the app) but there is **no auto-start after reboot** yet. Start OpenSMS once after rebooting the phone.
- OpenSMS is not the default SMS app. Your normal messaging app still receives messages. Some OEM battery managers may block background SMS delivery; allow OpenSMS to run in the background if webhooks stop arriving.
- Only SMS is forwarded, not MMS.
- GCM/Play policy does not apply here; the app is distributed as an APK, but `RECEIVE_SMS` is a sensitive permission, so the "restricted settings" step from the Android install guide is required on newer versions.


## 📚 Tech Stack Used

### Python stack
It works on `Android` and will bring this to `Linux` & `Windows` too.
- [FastAPI](https://github.com/fastapi/fastapi) for the API layer.
- [KivyMD](https://github.com/kivymd/kivymd) and few [Kivy](https://github.com/kivy) components for the UI and building the app.
- SMS via [pyjnius](https://github.com/kivy/pyjnius) on `Android`.
- [PySerial](https://www.pyserial.com/docs) on Desktop platforms to send `AT` commands to LTE/GSM development board.
- [psutil](https://psutil.readthedocs.io/stable/) to get the network interface details on Desktop platforms.

### 📋 Future plans

1. Leverage the `ESP32-S3` to host the SMS API server directly without the need of an Android phone or a Desktop platform (for low traffic).


## 💌 Contact Details
- Developer & Author: [Somnath Das](https://cv.daslearning.in/)
- [Contact Us](https://daslearning.in/contact/)

-----

## 🧩 Building your app from code

### 📱 Build Android App
A Linux environment is recommended for the app development. If you are on Windows, you may use `WSL` or any `Virtual Machine`.

```bash
# add the python repository (Optional: if you want some other versions)
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update

# install all dependencies.
sudo apt install -y ant autoconf automake ccache cmake g++ gcc lbzip2 libffi-dev libltdl-dev libtool libssl-dev make openjdk-17-jdk patch patchelf pkg-config unzip wget zip git python3 python3-venv python3-dev libcairo2-dev pkg-config libjpeg-dev zlib1g-dev

# optionally you may check the java installation with below commands
java -version
javac -version

# Rust is needed to build pydantic-core for Android (use rustup, not the distro rust package)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
source "$HOME/.cargo/env"

# install python modules
git clone https://github.com/daslearning-org/OpenSMS.git
cd OpenSMS/app
python3 -m venv .env # create python virtual environment
source .env/bin/activate
pip install -r req_android.txt

# build the android apk
buildozer android debug # this may take a good amount of time for the first time & will generate the apk in the bin directory
```
