import re
import time

try:
    from kivy.utils import platform
except ImportError:
    import sys
    if sys.platform.startswith("win"):
        platform = "win"
    elif sys.platform.startswith("linux"):
        platform = "linux"
    else:
        platform = "unknown"

## OS specific setups
# for android
if platform == "android":
    from jnius import autoclass
else:
    import serial

## functions

def clean_and_validate_phone(phone_number:str) -> str | None:
    """
    Cleans a phone number by removing spaces, dashes, parentheses, etc.,

    and validates if it follows the international format starting with '+'.

    Returns:
        - The cleaned string (e.g., '+14155552671') if valid.
        - None if the format is invalid.
    """
    if not isinstance(phone_number, str):
        return None

    # Step 1: Remove all characters except digits and the leading '+'
    cleaned = re.sub(r"[^\d+]", "", phone_number)

    # Step 2: Validate using E.164 international standard regex
    # - Starts with '+'
    # - Followed by 1 to 15 digits (country code + subscriber number)
    pattern = r"^\+[1-9]\d{1,14}$"

    if re.match(pattern, cleaned):
        return cleaned

    return None


class SmsCrossPlatform:

    def __init__(
            self, 
            serial_port: str | None = None,
            baud_rate: int | None = None,
            timeout: int = 3,
            write_timeout: int = 5
        ):
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.serial_conn = None
        self.serial_ok = False
        if platform != "android":
            if serial_port and baud_rate:
                try:
                    self.serial_conn = serial.Serial(
                        port=serial_port,
                        baudrate=baud_rate,
                        timeout=timeout,
                        write_timeout=write_timeout
                    )
                    time.sleep(1)
                    self.serial_conn.reset_input_buffer()
                    init_resp = self.send_at_command("AT", "OK")
                    sms_text_mode = self.send_at_command("AT+CMGF=1", "OK")
                    if "OK" in init_resp and "OK" in sms_text_mode:
                        self.serial_ok = True
                        print("Seiral GSM Module is initialized successfully.")
                except Exception as e:
                    print(f"Error while connecting serial port: {e}")
                    self.serial_conn = None

    def read_response(self, exp_resp, timeout=5):
        """Read modem response until timeout."""
        response = ""
        start = time.time()

        while time.time() - start < timeout:
            if self.serial_conn.in_waiting:
                data = self.serial_conn.read(self.serial_conn.in_waiting)
                response += data.decode("utf-8", errors="replace")

                if "OK" in response or \
                   "ERROR" in response or \
                   "+CMS ERROR:" in response or \
                   exp_resp in response:
                    break
            time.sleep(0.05)

        return response.strip()

    def send_at_command(self, at_cmd:str, exp_resp:str):
        """
        Send AT command to the modem
        """
        response = None
        if self.serial_conn:
            self.serial_conn.reset_input_buffer()
            at_cmd = f"{at_cmd}\r\n"
            self.serial_conn.write(at_cmd.encode(encoding="utf-8"))
            response = self.read_response(exp_resp, self.timeout)
        return response

    def send_serial_sms(self, phone:str, msg:str):
        stat = False
        command = f'AT+CMGS="{phone}"'.encode(encoding="utf-8")
        set_phone_cmd = self.send_at_command(command, ">")
        if ">" in set_phone_cmd:
            sms_cmd = f"{msg}\r".encode(encoding="utf-8") + b"\x1A" # Ctrl+Z i.e. chr(26)
            self.serial_conn.write(sms_cmd)
            sms_result = self.read_response("OK", 10)
            if "+CMGS:" in sms_result and "OK" in sms_result:
                print(f"SMS sent from modem with response: {sms_result}")
                stat = True
        return stat

    def send_sms(self, phone:str, msg:str) -> bool:
        """
        Sends SMS from your default SIM as set in settings.

        Args:
            - phone (str) : The phone number starting with country code. Ex: +14155552671
            - msg (str) : The actual text message which needs to be sent.

        Returns:
            - bool: True if the execution is successful
        """
        stat = False
        if platform == "android":
            try:
                SmsManager = autoclass('android.telephony.SmsManager')
                sms_manager = SmsManager.getDefault()
                parts = sms_manager.divideMessage(msg)
                if parts.size() > 1:
                    sms_manager.sendMultipartTextMessage(phone, None, parts, None, None)
                else:
                    sms_manager.sendTextMessage(phone, None, msg, None, None)
                stat = True
            except Exception as e:
                print("SMS ❌ Failed:", e)
        else:
            if self.serial_conn:
                stat = self.send_serial_sms(phone, msg)
        return stat
