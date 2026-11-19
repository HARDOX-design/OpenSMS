from os.path import join, abspath, exists, dirname
from threading import Thread

import json
import time
import sys
import signal


# OS specific
try:
    from kivy.utils import platform
except ImportError:
    if sys.platform.startswith("win"):
        platform = "win"
    elif sys.platform.startswith("linux"):
        platform = "linux"
    else:
        platform = "unknown"

# local imports
from services.postApi import (
    PosiApiServer,
    set_api_logger,
    set_serial_port,
    log_activities,
)
from services.logSvc import LogService
from services.config import app_name
from services.webhookSvc import (
    WebhookDispatcher,
    build_inbound_payload,
    drain_inbox_file,
)

# global vars
resp_template = {
    "server": "none",
    "serial": "none",
    "provider": "none",
}
req_data = {}
api_started = False
serial_started = False
serial_mgr = None
sms_receiver = None
sms_receiver_context = None
sms_register_attempt = 0
sms_last_attempt = 0.0
log_obj = LogService(app_name, platform)

running = True
def request_stop(signum=None, frame=None):
    global running
    print("Stop signal received")
    running = False

# Stop signal handle
signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)

## OS specific setups

# for android
if platform == "android":
    from android.storage import app_storage_path
    from jnius import autoclass
    base_path = app_storage_path()
#for other platforms like windows or linux
else:
    from services.pathMgr import get_user_data_dir
    from services.serialMgr import SerialConDesktop
    base_path = get_user_data_dir(app_name)

config_dir = join(base_path, 'config')
if not exists(config_dir):
    import os
    os.makedirs(config_dir, exist_ok=True)
set_api_logger(log_obj)
req_file = join(config_dir, "request.json")
resp_file = join(config_dir, "resp.json")
inbox_file = join(config_dir, "inbox.jsonl")
outbox_file = join(config_dir, "outbox.json")
webhook_status_file = join(config_dir, "webhook_status.json")
app_config_file = join(config_dir, "config.json")

## global obj
app_api_server = PosiApiServer()
webhook_dispatcher = WebhookDispatcher(
    config_path=app_config_file,
    outbox_path=outbox_file,
    status_path=webhook_status_file,
    log=log_activities,
)

## functions
def write_resp():
    """ Write the response json on file. """
    global resp_template
    with open(resp_file, "w") as rf:
        json.dump(resp_template, rf)

def read_req_file():
    """ Read the command file from file (Instrunctions from main app). """
    global req_data
    if exists(req_file):
        try:
            with open(req_file, "r") as rf:
                req_data = json.load(rf)
        except Exception as e:
            print(f"Error while reading config from svc: {e}")

def register_sms_receiver():
    """Register the Android SMS_RECEIVED receiver in this service process.

    Buildozer ignores `android.extra_manifest_application_xml`, so the receiver
    cannot be declared in the manifest. Registering it at runtime keeps the
    receiver alive for as long as the (sticky) service runs.
    """
    global sms_receiver, sms_receiver_context, sms_register_attempt
    if platform != "android" or sms_receiver:
        return
    sms_register_attempt += 1
    try:
        PythonService = autoclass("org.kivy.android.PythonService")
        service = PythonService.mService
        if service is None:
            return
        context = service.getApplicationContext()
        IntentFilter = autoclass("android.content.IntentFilter")
        SmsReceiver = autoclass("in.daslearning.opensms.SmsReceiver")
        receiver = SmsReceiver()
        intent_filter = IntentFilter("android.provider.Telephony.SMS_RECEIVED")
        context.registerReceiver(receiver, intent_filter)
        sms_receiver = receiver
        sms_receiver_context = context
        print("Inbound SMS receiver registered")
        log_activities("Inbound SMS receiver registered")
    except Exception as e:
        if sms_register_attempt == 1 or sms_register_attempt % 50 == 0:
            print(f"Could not register the inbound SMS receiver: {e}")
            log_activities(f"Could not register the inbound SMS receiver: {e}")

def unregister_sms_receiver():
    global sms_receiver, sms_receiver_context
    if sms_receiver and sms_receiver_context:
        try:
            sms_receiver_context.unregisterReceiver(sms_receiver)
            print("Inbound SMS receiver unregistered")
        except Exception as e:
            print(f"Could not unregister the inbound SMS receiver: {e}")
    sms_receiver = None
    sms_receiver_context = None

def api_server_control(api_stat: str):
    """ Turns API server on/off """
    global api_started
    global resp_template
    global req_data
    print(req_data)
    if api_stat == "stop":
        app_api_server.stop()
        api_started = False
        resp_template["server"] = "stopped"
    elif api_stat == "start":
        app_api_server.start(req_data["port"])
        api_started = True
        resp_template["server"] = "started"
    Thread(target=write_resp, daemon=True).start()

def serial_conn_control(cmd:str):
    global serial_started
    global serial_mgr

    if not serial_mgr:
        serial_mgr = SerialConDesktop()

    if serial_mgr:
        if cmd == "start":
            port = req_data.get("serial_port", None)
            baud = req_data.get("baudrate", 115200)
            if port:
                serial_started, provider = set_serial_port(
                    port=port,
                    baud_rate=baud
                )
                if serial_started:
                    resp_template["serial"] = "ok"
                    resp_template["provider"] = provider
        elif cmd == "stop":
            pass # To be implemented later
        Thread(target=write_resp, daemon=True).start()


def process_inbound_inbox():
    """Forward every inbound SMS persisted by the Android receiver."""
    for message in drain_inbox_file(inbox_file):
        payload = build_inbound_payload(message)
        log_activities(f"Inbound SMS received from: {payload['from']}")
        if webhook_dispatcher.enqueue(payload):
            print(f"Inbound SMS queued for webhook from: {payload['from']}")
        else:
            print(
                "Inbound SMS not forwarded (webhook disabled or target "
                f"invalid) from: {payload['from']}"
            )


def app_service_thread():
    """
    The main service thready runs forever till the app is running & performs the operations.
    """
    #global vars
    global api_started, sms_last_attempt

    register_sms_receiver()
    write_resp() # blank the old file

    # keep alive the service & check for requests
    try:
        while running:
            read_req_file()

            # retry the SMS receiver until RECEIVE_SMS has been granted
            if not sms_receiver and time.time() - sms_last_attempt >= 10:
                sms_last_attempt = time.time()
                register_sms_receiver()

            # handle api server
            server_stat = req_data.get("server", "")
            if not api_started and server_stat == "start":
                api_server_control("start")
            elif api_started and server_stat == "stop":
                api_server_control("stop")

            # handle serial request
            if platform != "android":
                serial_req = req_data.get("serial_port", "none")
                if not serial_started and serial_req != "none":
                    serial_conn_control("start")
                elif serial_started and serial_req == "none":
                    serial_conn_control("stop")

            # forward inbound SMS to the configured webhook
            process_inbound_inbox()

            # put a sleep
            time.sleep(0.2)
    # Exceptions and closure
    except Exception as e:
        print(f"Error in service loop: {e}")
    finally:
        # Stop the API server if it is still running
        if api_started:
            api_server_control("stop")
        unregister_sms_receiver()
        webhook_dispatcher.stop()
        print("OpenSMS service stopped")


# run locally
if __name__ == "__main__":
    app_service_thread()
