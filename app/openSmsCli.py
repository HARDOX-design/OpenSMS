""" OpenSMS - CLI: Only works on Desktop platforms """

import os
os.environ["KIVY_NO_ARGS"] = "1" # don't use kivy args

import sys
import argparse
from threading import Thread, Event
import time
import signal
import subprocess

# local imports
from services.postApi import PosiApiServer, set_api_logger, set_serial_port
from services.logSvc import LogService
from services.config import app_name

# OS specific
if sys.platform.startswith("win"):
    platform = "win"
elif sys.platform.startswith("linux"):
    platform = "linux"
else:
    platform = "unknown"

# Global vars / obj
log_obj = LogService(app_name, platform)
set_api_logger(log_obj)

# functions
def logger(message:str):
    Thread(
        target=log_obj.write_log,
        kwargs={
            "message": message
        },
        daemon=True
    ).start()

def start_background(serial, baud, host, port):
    script = os.path.abspath(__file__)

    cmd = [
        sys.executable,
        script,
        "run",
        "--serial", serial,
        "--baud", baud,
        "--host", host,
        "--port", port
    ]

    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True
    }

    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP |
            subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(cmd, **kwargs)
    print(f"OpenSMS started in background. PID: {process.pid}")

# classes

class OpenSmsCliCls:

    def __init__(self):
        self.serial_ok = False
        self.api_server = None

    def start_serial(self, port:str, baud:int):
        self.serial_ok, provider = set_serial_port(
            port=port,
            baud_rate=baud
        )
        if self.serial_ok:
            print(f"Modem connection success with operator: {provider}")
            logger(f"Modem connection success with operator: {provider}")
        return self.serial_ok

    def start_api_server(self, host:str = '0.0.0.0', port:int = 9292):
        if not self.api_server:
            self.api_server = PosiApiServer(host, port)
            self.api_server.start()

    def stop_api_server(self):
        if self.api_server:
            self.api_server.stop()

    def new_api_key(self):
        from services.postApi import generate_new_api_key
        api_key = generate_new_api_key()
        print(f"\nAPI Key: {api_key}\n")

    def command_loop(self):
        print("\nOpenSMS CLI is running.")
        print("Available commands: newkey, status, help, quit\n")
        help_cmd = """
        Available commands:
          newkey   - Generate New API Key
          status   - Show service status
          help     - Show available commands
          quit     - Stop OpenSMS
        """

        while True:
            try:
                cmd = input("OpenSMS> ").strip().lower()

                if cmd in ("quit", "exit"):
                    print("Stopping OpenSMS...")
                    break

                elif cmd == "newkey":
                    confirmation = input("Old key will be invalid, are you sure (y/n) > ").strip().lower()
                    if confirmation == "y":
                        self.new_api_key()

                elif cmd == "status":
                    print(f"Modem connected: {self.serial_ok}")
                    print(f"API server running: {self.api_server is not None}")

                elif cmd == "help":
                    print(help_cmd)

                elif cmd:
                    print(f"Unknown command: {cmd}")

            except EOFError:
                break
            except KeyboardInterrupt:
                break
        self.stop_api_server()
        print("OpenSMS stopped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenSMS CLI")
    parser.add_argument(
        '--serial',
        type=str,
        help="Serial port. Ex: `/dev/ttyUSB0` on Linux or `COM1` on Windows."
    )
    parser.add_argument(
        '--baud',
        type=int,
        default=115200,
        help="Baud rate for the serial port. Default: 115200"
    )
    parser.add_argument(
        '--host',
        type=str,
        default='0.0.0.0',
        help="Host IP to run the API server. Default: 0.0.0.0"
    )
    parser.add_argument(
        '--port',
        type=int,
        default=9292,
        help="Port number for API server, Default: 9292"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["start", "run", "interactive"],
        default="run",
        help="start: background service, run: foreground service, interactive: query based operations"
    )

    args = parser.parse_args()

    if not args.serial:
        raise RuntimeError("Serial port is needed, user --serial or try --help")

    serial = args.serial
    baud = args.baud if args.baud else 115200
    host = args.host if args.host else '0.0.0.0'
    port = args.port if args.port else 9292

    # background start
    if args.command == "start":
        start_background(serial, baud, host, port)
        sys.exit(0)

    sms_mgr = OpenSmsCliCls()

    if args.command == "run":
        # foreground process
        serial_ok = sms_mgr.start_serial(serial, baud)
        #if not serial_ok:
        #    raise RuntimeError(f"Could not start the serial modem on: {serial}")

        sms_mgr.start_api_server(host, port)

        # Keep alive the service
        #sms_mgr.command_loop()

        # Main process stays alive
        stop_event = Event()
        def shutdown_handler(signum, frame):
            stop_event.set()
        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)

        try:
            print("OpenSMS service running...")
            stop_event.wait()

        finally:
            print("Stopping OpenSMS...")
            sms_mgr.stop_api_server()
            print("OpenSMS stopped.")

    elif args.command == "interactive":
        # interactive operations
        serial_ok = sms_mgr.start_serial(serial, baud)
        #if not serial_ok:
        #    raise RuntimeError(f"Could not start the serial modem on: {serial}")

        sms_mgr.start_api_server(host, port)
        # Keep alive the service
        sms_mgr.command_loop()

# End