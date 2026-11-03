""" Some useful Serial tools on Desktop platform """

import time
import serial
import serial.tools.list_ports as list_ports

# Functions

def get_com_ports() -> list:
    """
    Get a list of active COM ports connected to the computer
    """
    com_ports = []
    ports = list_ports.comports()
    if ports:
        for port in ports:
            hwid = port.hwid
            if hwid == "n/a" or hwid == "":
                continue
            else:
                com_ports.append(port.device)
        com_ports.sort()
    return com_ports

# Classes

class SerialConDesktop:

    def __init__(self):
        self.serial_conn = None
        self.serial_ok = False
        self.timeout = 3
        self.write_timeout = 5

    def initialize_gsm_module(
        self, 
        serial_port: str | None = None,
        baud_rate: int | None = 115200,
        timeout: int = 3,
        write_timeout: int = 5
    ) -> bool:
        self.timeout = timeout
        self.write_timeout = write_timeout
        if serial_port:
            try:
                # python3 -m serial.tools.list_ports -v
                self.serial_conn = serial.Serial(
                    port=serial_port,
                    baudrate=baud_rate,
                    timeout=timeout,
                    write_timeout=write_timeout
                )
                time.sleep(0.5)
                self.serial_conn.reset_input_buffer()
                init_resp = self.send_at_command("AT", "OK")
                sms_text_mode = self.send_at_command("AT+CMGF=1", "OK")
                sms_gsm_set = self.send_at_command('AT+CSCS="GSM"', "OK")
                if "OK" in init_resp \
                    and "OK" in sms_text_mode:
                        self.serial_ok = True
                        print("Seiral GSM Module is initialized successfully.")
                else:
                    self.serial_conn.close()
                    self.serial_ok = False
                    self.serial_conn = None
            except Exception as e:
                print(f"Error while connecting serial port: {e}")
                self.serial_conn = None
        return self.serial_ok

    def read_response(self, exp_resp, timeout=3):
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
            #self.serial_conn.reset_input_buffer()
            at_cmd = f'{at_cmd}\r\n'
            self.serial_conn.write(at_cmd.encode(encoding="utf-8"))
            response = self.read_response(exp_resp, self.timeout)
            print(response)
        return response

    def send_serial_sms(self, phone:str, msg:str):
        stat = False
        command = f'AT+CMGS="{phone}"' # set the phone numer for sms
        set_phone_cmd = self.send_at_command(command, ">")
        #print(set_phone_cmd)
        if ">" in set_phone_cmd:
            sms_cmd = f"{msg}\r".encode(encoding="utf-8") + b"\x1A" # Ctrl+Z i.e. chr(26)
            self.serial_conn.write(sms_cmd)
            time.sleep(0.2)
            sms_result = self.read_response("OK", 10)
            print(sms_result)
            if "+CMGS:" in sms_result and "OK" in sms_result:
                print(f"SMS sent from modem with response: {sms_result}")
                stat = True
            self.serial_conn.flush()
        return stat

