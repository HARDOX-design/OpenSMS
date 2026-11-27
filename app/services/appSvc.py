from os.path import join, abspath, exists, dirname
from threading import Thread

import os
import signal
import sys
import time


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
    set_runtime_providers,
    log_activities,
)
from services.logSvc import LogService
from services.config import app_name
from services import controlSvc, settingsSvc
from services.webhookSvc import (
    WebhookDispatcher,
    ack_inbox_message,
    build_inbound_payload,
    claim_inbox_messages,
)

# global vars
resp_template = {
    "server": "none",
    "port": None,
    "serial": "none",
    "provider": "none",
}
req_data = {}
api_started = False
serial_started = False
# True when a handled Restart could not be normalised on disk; the loop then
# suppresses that same intent instead of restarting again.
restart_intent_handled = False
handled_restart_id = None
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
inbox_dir = join(config_dir, "inbox")
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
# Let the API report live facts: the port the server is bound to, whether it is
# running, and how many Webhooks are waiting in the Outbox.
set_runtime_providers({
    "running": lambda: app_api_server.bound_port is not None,
    "bound_port": lambda: app_api_server.bound_port,
    "outbox_pending": lambda: webhook_dispatcher.pending_count(),
})

## functions
def write_resp():
    """ Write the response json on file. """
    global resp_template
    settingsSvc.atomic_write_json(resp_file, resp_template)

def read_req_file():
    """ Read the control file (intents from the API or the main app). """
    global req_data, restart_intent_handled, handled_restart_id
    if not exists(req_file):
        return
    req_data = controlSvc.read_control(config_dir)
    intent = req_data.get(controlSvc.INTENT_KEY)
    if restart_intent_handled and controlSvc.matches_restart(
        req_data, handled_restart_id
    ):
        # A handled Restart could not be persisted as "start"; do not let the
        # same stale intent restart the server again. A later Restart carries a
        # new id and is handled normally.
        req_data[controlSvc.INTENT_KEY] = controlSvc.INTENT_START
    elif intent != controlSvc.INTENT_RESTART:
        restart_intent_handled = False
        handled_restart_id = None

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

def stop_api_server() -> bool:
    """ Stop the API server, reporting whether it exited in time. """
    global api_started
    stopped = app_api_server.stop()
    api_started = False
    return stopped

def api_server_control(api_stat: str, port: int | None = None):
    """ Turns API server on/off """
    global api_started, resp_template
    print(req_data)
    if api_stat == "stop":
        if stop_api_server():
            resp_template["server"] = "stopped"
            resp_template["port"] = None
    elif api_stat == "start":
        # Settings own the port; the control file is only a fallback.
        start_port = (
            port if port is not None else controlSvc.effective_port(config_dir)
        )
        app_api_server.start(start_port)
        api_started = True
        resp_template["server"] = "started"
        resp_template["port"] = start_port
    Thread(target=write_resp, daemon=True).start()

def restart_api_server():
    """ Stop once, then start again on the effective port.

    The control intent is normalised back to "start" after the new server has
    been asked to start, so the 0.2 s poll cannot trigger the Restart again.
    """
    global req_data, restart_intent_handled, handled_restart_id
    port = controlSvc.effective_port(config_dir)
    req_data["port"] = port
    restart_id = req_data.get(controlSvc.RESTART_ID_KEY)
    if not stop_api_server():
        # Never add a second listener while the previous one is still up; the
        # intent stays "restart" so the next poll retries the stop.
        print("Restart deferred: the previous server has not stopped yet")
        return
    time.sleep(0.5)
    api_server_control("start", port)
    try:
        controlSvc.normalise_restart(config_dir, port)
        restart_intent_handled = False
        handled_restart_id = None
    except Exception as e:
        restart_intent_handled = True
        handled_restart_id = restart_id
        print(f"Could not normalise the Restart intent: {e}")
        log_activities(f"Could not normalise the Restart intent: {e}")

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
    """Forward every inbound SMS claimed from the receiver's spool directory."""
    if platform != "android":
        # Works on only android for now.
        return
    for item in claim_inbox_messages(inbox_dir):
        payload = build_inbound_payload(item["message"])
        log_activities(f"Inbound SMS received from: {payload['from']}")
        try:
            queued = webhook_dispatcher.enqueue(payload)
        except Exception as e:
            # Keep the .processing file so the next tick retries it.
            print(f"Could not enqueue inbound SMS from {payload['from']}: {e}")
            continue
        if queued:
            print(f"Inbound SMS queued for webhook from: {payload['from']}")
        else:
            print(
                "Inbound SMS not forwarded (webhook disabled or target "
                f"invalid) from: {payload['from']}"
            )
        # Only acknowledge once the Outbox has persisted the delivery.
        ack_inbox_message(item["path"])


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
            server_stat = req_data.get(controlSvc.INTENT_KEY, "")
            if server_stat == controlSvc.INTENT_RESTART:
                restart_api_server()
            elif not api_started and server_stat == controlSvc.INTENT_START:
                api_server_control("start")
            elif api_started and server_stat == controlSvc.INTENT_STOP:
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
