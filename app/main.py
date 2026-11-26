import os
os.environ['KIVY_GL_BACKEND'] = 'sdl2'
#from kivy.config import Config
#Config.set("graphics", "multisamples", "0")
import sys
import json
import tempfile
from threading import Thread, Lock

from kivy.uix.widget import Widget
from kivy.core.clipboard import Clipboard
from kivy.lang import Builder
from kivy.core.window import Window
from kivy.utils import platform
from kivy.metrics import dp, sp
from kivy.clock import Clock
from kivy.properties import StringProperty, NumericProperty, ObjectProperty, BooleanProperty, ColorProperty

# debug
try:
    from kivy.graphics.opengl import glGetString, GL_VENDOR, GL_RENDERER, GL_VERSION
    print("GL_VENDOR:", glGetString(GL_VENDOR))
    print("GL_RENDERER:", glGetString(GL_RENDERER))
    print("GL_VERSION:", glGetString(GL_VERSION))
except Exception as e:
    print(f"OpenGL query error: {e}")

from kivymd.uix.widget import MDWidget
from kivymd.app import MDApp
from kivymd.uix.navigationbar import MDNavigationBar, MDNavigationItem
from kivymd.uix.screen import MDScreen
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDButton, MDButtonIcon, MDButtonText
from kivymd.uix.menu import MDDropdownMenu
from kivymd.uix.label import MDLabel
from kivymd.uix.dialog import MDDialog, MDDialogHeadlineText, MDDialogSupportingText, MDDialogButtonContainer, MDDialogContentContainer

# import screens (else pyinstaller may ignore it)
from screens.mainScr import ButtonControl, EndPointUrl, DocButton, SerialCard
from screens.settings import SettingsBox, ApiKeyTxtbox

# import services
from services import settingsSvc
from services.postApi import has_api_keys, generate_new_api_key
from services.netSvc import get_network_ips
from services.config import __version__, app_name
from services.webhookSvc import (
    default_webhook_config,
    format_webhook_status,
    is_valid_target,
    normalize_webhook_config,
    read_webhook_status,
    send_test_webhook,
)

# IMPORTANT: Set this property for keyboard behaviour
Window.softinput_mode = "below_target"

# Save, Reset and permission confirmation write config from separate threads.
config_write_lock = Lock()

# import platform specific modules
if platform == "android":
    from jnius import autoclass, cast
    from android.permissions import check_permission, request_permissions, Permission
    from screens.kivymd_patch import disable_gpu_ripple, disable_hover
    from services.android_env import on_android
    disable_gpu_ripple()
    if on_android():
        disable_hover()
else:
    from services.appSvc import app_service_thread
    from services.pathMgr import get_user_data_dir
    from services.serialMgr import get_com_ports

# Local classes
class BaseMDNavigationItem(MDNavigationItem):
    icon = StringProperty()
    text = StringProperty()

class MainScreenBox(MDBoxLayout):
    top_pad = NumericProperty(0)
    bottom_pad = NumericProperty(0)
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if platform == "android":
            try:
                from android.display_cutout import get_height_of_bar
                self.top_pad = int(get_height_of_bar('status'))
                self.bottom_pad = int(get_height_of_bar('navigation'))
            except Exception as e:
                print(f"Failed android 15 padding: {e}")
                self.top_pad = 32
                self.bottom_pad = 48

## Global definitions

# Determine the base path for your application's resources
if getattr(sys, 'frozen', False):
    # Running as a PyInstaller bundle
    base_path = sys._MEIPASS
else:
    # Running in a normal Python environment
    base_path = os.path.dirname(os.path.abspath(__file__))
kv_file_path = os.path.join(base_path, 'main_layout.kv')

## Main App Class
class OpenSmsApp(MDApp):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        Window.bind(on_keyboard=self.events)
        self.resp_thred = None
        self.txt_dialog = None
        self.wake_lock = None
        self.service_stat = False
        self.api_started = False
        self.key_exists = False
        self.api_key = None
        self.serial_started = False
        self.serial_dropdown_menu = None
        self.webhook_status_path = None
        self.webhook_switch_guard = False
        self.port_loaded = None
        self.boot_loaded = False
        self.webhook_enabled_loaded = False
        self.webhook_url_loaded = ""
        self.webhook_secret_loaded = ""
        self.config_template = {
            "app": app_name,
            "port": 9292,
            "boot": False,
            "permissions": False,
            "webhook": default_webhook_config(),
        }
        self.req_template = {
            "port": 9292,
            "server": "stop",
            "serial_port": "none",
            "baudrate": 115200,
        }

    def build(self):
        self.theme_cls.primary_palette = "Blue"
        self.theme_cls.accent_palette = "Orange"
        return Builder.load_file(kv_file_path)

    ## App logics
    def on_start(self):
        # paths setup
        self.api_started = False
        file_m_height = 1
        # for android
        if platform == "android":
            file_m_height = 0.9
            self.root.ids.home_scr.ids.serial_card.opacity = 0
            from android.storage import app_storage_path
            sdk_version = 33
            try:
                VERSION = autoclass('android.os.Build$VERSION')
                sdk_version = VERSION.SDK_INT
                print(f"Android SDK: {sdk_version}")
                #self.show_toast_msg(f"Android SDK: {sdk_version}")
            except Exception as e:
                print(f"Could not check the android SDK version: {e}")
                #self.show_toast_msg(f"Error checking SDK: {e}", is_error=True)
            self.android_permissions = [
                Permission.WAKE_LOCK,
                Permission.SEND_SMS,
                Permission.RECEIVE_SMS,
            ]
            if sdk_version >= 33:
                self.android_permissions.extend([Permission.POST_NOTIFICATIONS])
            request_permissions(self.android_permissions)
            self.internal_storage = app_storage_path()
            self.config_dir = os.path.join(self.internal_storage, 'config')
            os.makedirs(self.config_dir, exist_ok=True)
            self.request_path = os.path.join(self.config_dir, "request.json")
            self.resp_path = os.path.join(self.config_dir, "resp.json")
            svc_running = False
            # start the listner service on Android
            try:
                svc_running = self.is_service_running()
                if not svc_running:
                    self.write_request() # cleans the request file
                    self.start_service()
                else:
                    self.service_stat = True
            except Exception as e:
                print(f"Error while starting android service: {e}")
        # non android platforms
        else:
            self.service_stat = True
            # Path related set up
            self.internal_storage = get_user_data_dir(self.config_template["app"])
            self.config_dir = os.path.join(self.internal_storage, 'config')
            os.makedirs(self.config_dir, exist_ok=True)
            self.request_path = os.path.join(self.config_dir, "request.json")
            self.resp_path = os.path.join(self.config_dir, "resp.json")
            self.write_request()
            # Serial setup
            ports = get_com_ports()
            if len(ports) >= 1:
                serial_card = self.root.ids.home_scr.ids.serial_card
                serial_card.disabled = False
                menu_items = [
                    {
                        "text": f"{port}",
                        "on_release": lambda x=f"{port}": self.serial_port_select(x),
                    } for port in ports
                ]
                self.serial_dropdown_menu = MDDropdownMenu(
                    caller=serial_card, items=menu_items, position="center"
                )
            # Start the app service
            Thread(target=app_service_thread, daemon=True).start()
        # All generic things
        self.app_config = os.path.join(self.config_dir, "config.json")
        self.webhook_status_path = os.path.join(
            self.config_dir, "webhook_status.json"
        )
        auto_start_sw = self.root.ids.settings_scr.ids.auto_start_sw
        if os.path.exists(self.app_config):
            saved_config = settingsSvc.load_settings(self.app_config)
            self.config_template = saved_config
            self.config_template["port"] = int(
                saved_config.get("port") or settingsSvc.DEFAULT_PORT
            )
            self.config_template["boot"] = bool(saved_config.get("boot"))
            self.config_template["permissions"] = bool(
                saved_config.get("permissions", False)
            )
            webhook_cfg = normalize_webhook_config(saved_config.get("webhook"))
            print(
                "start config check: "
                f"port={self.config_template['port']}, "
                f"boot={self.config_template['boot']}, "
                f"webhook enabled={webhook_cfg['enabled']}, "
                f"target={webhook_cfg['url']}"
            )
            self.req_template["port"] = int(self.config_template["port"])
            start_on_launch = self.config_template["boot"]
            if start_on_launch:
                self.req_template["server"] = "start"
                auto_start_sw.active = True
                Thread(target=self.write_request, daemon=True).start()
            else:
                auto_start_sw.active = False
        else:
            self.write_app_conf()
        self.refresh_settings_view()
        self.check_if_key_exists()
        self.stop_resp_loop()
        self.resp_thred = Thread(target=self.resp_check_loop, daemon=True)
        self.resp_thred.start()
        print("Init success...")

    def is_service_running(self):
        if platform == "android":
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Context = autoclass('android.content.Context')
            ActivityManager = autoclass('android.app.ActivityManager')
            activity = PythonActivity.mActivity
            manager = activity.getSystemService(
                Context.ACTIVITY_SERVICE
            )
            services = manager.getRunningServices(1000)
            for service_info in services.toArray():
                if str(service_info.service.getClassName()) == (
                    'in.daslearning.opensms.ServiceOpensmssvc'
                ):
                    return True
            return False
        else:
            return False

    def start_service(self):
        if platform == "android":
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            mActivity = PythonActivity.mActivity
            service = autoclass('in.daslearning.opensms.ServiceOpensmssvc')
            #argument = ''
            argument = os.environ.get('PYTHON_SERVICE_ARGUMENT', '')
            print(f"PYTHON_SERVICE_ARGUMENT: {argument}")
            #service.start(mActivity, argument)
            service.start(mActivity, 'icon', 'OpenSMS API', 'Service Running' , argument)
        self.service_stat = True

    def stop_service(self):
        if platform == "android":
            service = autoclass('in.daslearning.opensms.ServiceOpensmssvc')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            mActivity = PythonActivity.mActivity
            service.stop(mActivity)
            self.service_stat = False

    def resp_check_loop(self):
        self.api_started = False
        self.serial_started = False
        import time
        api_srv_btn = self.root.ids.home_scr.ids.api_srv_btn
        api_srv_lbl = self.root.ids.home_scr.ids.api_srv_lbl
        serial_btn = self.root.ids.home_scr.ids.serial_btn
        serial_stat_lbl = self.root.ids.home_scr.ids.serial_stat_lbl
        time.sleep(0.5) # A delay for the android service to kick in
        # debug
        print_path = os.path.abspath(self.resp_path)
        print(f"RESP Path: {print_path}")
        while True:
            resp = None
            if os.path.exists(self.resp_path):
                try:
                    with open(self.resp_path, "r", encoding="utf-8") as rf:
                        resp = json.load(rf)
                    if resp:
                        # server status
                        server_stat = resp.get("server", "unknown")
                        if server_stat == "started":
                            if not self.api_started:
                                self.api_started = True
                                api_srv_lbl.text = f"API Server running on port: {self.req_template["port"]}"
                                api_srv_btn.btn_icon = "stop"
                                api_srv_btn.btn_txt = "Stop Server"
                                api_srv_btn.md_bg_color = "orange"
                                Clock.schedule_once(lambda dt: self.update_api_details())
                                #Clock.schedule_once(lambda dt: self.show_toast_msg("API Server Started"))
                        elif server_stat == "stopped":
                            if self.api_started:
                                self.api_started = False
                                api_srv_lbl.text = "API Server is not running"
                                api_srv_btn.btn_icon = "play"
                                api_srv_btn.btn_txt = "Start Server"
                                api_srv_btn.md_bg_color = "green"
                                Clock.schedule_once(lambda dt: self.clear_api_details())
                                #Clock.schedule_once(lambda dt: self.show_toast_msg("API Server Stopped"))

                        # serial status
                        if platform != "android":
                            ser_stat = resp.get("serial", "none")
                            provider = resp.get("provider", "unknown")
                            if not self.serial_started:
                                if ser_stat == "ok":
                                    stat_text = f"Modem connected to: {provider}"
                                    serial_stat_lbl.text = stat_text
                                    serial_btn.btn_txt = "All Done"
                                    serial_btn.md_bg_color = "orange"
                    else:
                        api_srv_lbl.text = "Unable to get response from service"
                except Exception as e:
                    print(f"Error while reading resp: {e}")
            else:
                api_srv_lbl.text = "RESP Path doesn't exist!"
            time.sleep(2)

    def write_app_conf(self):
        with config_write_lock:
            file_descriptor, tmp_path = tempfile.mkstemp(
                prefix="config.",
                suffix=".tmp",
                dir=self.config_dir,
            )
            try:
                with os.fdopen(file_descriptor, "w") as cf:
                    json.dump(self.config_template, cf)
                os.replace(tmp_path, self.app_config)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    def write_request(self):
        with open(self.request_path, "w") as rf:
            json.dump(self.req_template, rf)

    def clear_api_details(self):
        endpoint_box = self.root.ids.home_scr.ids.endpoint_box
        endpoint_box.clear_widgets()

    def update_api_details(self):
        self.clear_api_details()
        ip_map = get_network_ips()
        port_no = self.config_template["port"]
        endpoint_box = self.root.ids.home_scr.ids.endpoint_box
        doc_btn = DocButton()
        endpoint_box.add_widget(doc_btn)
        iface_widget = EndPointUrl()
        iface_widget.url = f"http://127.0.0.1:{port_no}/sms"
        iface_widget.iface_name = "localhost"
        endpoint_box.add_widget(iface_widget)
        # WiFi interfaces
        for wifi_iface in ip_map["wifi"]:
            iface_name = wifi_iface.get("interface", "")
            ip_addr = wifi_iface["ip"]
            url = f"http://{ip_addr}:{port_no}/sms"
            iface_widget = EndPointUrl()
            iface_widget.url = url
            iface_widget.iface_name = f"WiFi: {iface_name}"
            endpoint_box.add_widget(iface_widget)
        # Ethernet interfaces
        for eth_iface in ip_map["ethernet"]:
            iface_name = eth_iface.get("interface", "")
            ip_addr = eth_iface["ip"]
            url = f"http://{ip_addr}:{port_no}/sms"
            iface_widget = EndPointUrl()
            iface_widget.url = url
            iface_widget.iface_name = f"Eth: {iface_name}"
            endpoint_box.add_widget(iface_widget)

    def api_server_mgr(self, instance=None):
        permissions_ok = self.check_request_android_permission()
        if not permissions_ok:
            return
        key_ok = self.check_if_key_exists()
        if key_ok:
            if not self.service_stat:
                self.start_service()
            if not self.api_started:
                self.req_template["server"] = "start"
                self.write_request()
            else:
                self.req_template["server"] = "stop"
                self.write_request()
        else:
            self.new_api_key_pop()

    def check_request_android_permission(self):
        if platform == "android" and not self.config_template["permissions"]:
            permission_flag = True
            for permission in self.android_permissions:
                tmp_flag = check_permission(permission)
                if not tmp_flag:
                    permission_flag = False
                    break
            if not permission_flag:
                request_permissions(self.android_permissions, self.permission_callback)
            return permission_flag
        else:
            return True

    def permission_callback(self, permissions, results):
        # results is a list of booleans corresponding to requested permissions
        usr_deny_flag = False
        if False in results:
            # the user checked "Don't ask again" or denied it twice.
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            current_activity = PythonActivity.mActivity
            # Check rationale status via Android API
            for permission in permissions:
                should_show = current_activity.shouldShowRequestPermissionRationale(permission)
                if not should_show:
                    usr_deny_flag = True
                    break
            if usr_deny_flag:
                # User denied twice / blocked permanently! Show redirect popup.
                print("Permission is denied by user!")
                Clock.schedule_once(lambda dt: self.show_settings_popup())

    def show_settings_popup(self):
        buttons = MDDialogButtonContainer(
            Widget(),
            MDButton(
                MDButtonText(
                    text="Cancel"
                ),
                style="text",
                on_release=self.txt_dialog_closer
            ),
            MDButton(
                MDButtonText(
                    text="Already granted",
                    theme_text_color= "Custom",
                    text_color= "orange"
                ),
                style="text",
                on_release=self.confirm_android_permissions
            ),
            MDButton(
                MDButtonText(
                    text="Open Settings",
                    theme_text_color= "Custom",
                    text_color= "green"
                ),
                style="text",
                on_release=self.open_android_settings
            ),
            spacing="8dp",
        )
        self.show_text_dialog(
            "Permissions Missing",
            "You need to grant the SMS (send & receive) & Notifications permissions from Settings.",
            None,
            buttons
        )

    def confirm_android_permissions(self, instnce=None):
        self.config_template["permissions"] = True
        Thread(target=self.write_app_conf, daemon=True).start()

    def open_android_settings(self, instance=None):
        self.txt_dialog_closer()
        # Target Android Native Intents
        Intent = autoclass('android.content.Intent')
        Settings = autoclass('android.provider.Settings')
        Uri = autoclass('android.net.Uri')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        # Create intent to open this specific app's settings details
        activity = PythonActivity.mActivity
        intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
        uri = Uri.fromParts("package", activity.getPackageName(), None)
        intent.setData(uri)
        # Launch settings
        activity.startActivity(intent)

    def check_if_key_exists(self):
        self.key_exists = has_api_keys()
        return self.key_exists

    def new_api_key_pop(self):
        buttons = MDDialogButtonContainer(
            Widget(),
            MDButton(
                MDButtonText(
                    text="Cancel"
                ),
                style="text",
                on_release=self.txt_dialog_closer
            ),
            MDButton(
                MDButtonText(
                    text="Generate",
                    theme_text_color= "Custom",
                    text_color= "green"
                ),
                style="text",
                on_release=self.create_new_api_key
            ),
            spacing="8dp",
        )
        self.show_text_dialog(
            "Generate API Key",
            "Do you want to generate the a new API key? This will make old keys invalid.",
            None,
            buttons
        )

    def create_new_api_key(self, instance=None):
        self.txt_dialog_closer()
        self.api_key = generate_new_api_key()
        if self.api_key:
            Clipboard.copy(self.api_key)
            custom = MDDialogContentContainer(
                ApiKeyTxtbox(
                    text = self.api_key
                )
            )
            buttons = MDDialogButtonContainer(
                Widget(),
                MDButton(
                    MDButtonText(
                        text="Cancel"
                    ),
                    style="text",
                    on_release=self.txt_dialog_closer
                ),
                MDButton(
                    MDButtonText(
                        text="Copy",
                        theme_text_color= "Custom",
                        text_color= "green"
                    ),
                    style="text",
                    on_release=self.copy_api_key
                ),
                spacing="8dp",
            )
            self.show_text_dialog(
                "Your New API Key",
                "The key is already copied, you can paste anywhere. This screen appears only once, make sure you copy the key securely!",
                custom,
                buttons
            )
        else:
            Clock.schedule_once(lambda dt: self.show_toast_msg("Failed to generate API key, try again!", is_error=True))

    def copy_api_key(self, instance=None):
        if self.api_key:
            Clipboard.copy(self.api_key)
        self.txt_dialog_closer()
        Clock.schedule_once(lambda dt: self.show_toast_msg("API Key has been copied, store it securely."))

    def api_doc_link(self, instance=None):
        port_no = self.config_template["port"]
        self.open_link(instance, f"http://127.0.0.1:{port_no}/docs")

    def open_link(self, instance=None, url=None):
        if url:
            import webbrowser
            webbrowser.open(url)

    ## Desktop specific setups

    def open_serial_menu(self, instance=None):
        if self.serial_dropdown_menu:
            self.serial_dropdown_menu.open()

    def serial_port_select(self, port:str):
        self.req_template["serial_port"] = port
        self.serial_dropdown_menu.dismiss()
        port_dropdown = self.root.ids.home_scr.ids.port_dropdown
        port_dropdown.text = port

    def start_serial_connection(self, instance=None):
        if self.serial_started:
            self.show_toast_msg("Modem is already connected")
            return
        port = self.req_template["serial_port"]
        if port == "none":
            self.show_toast_msg("Please select the Serial Port first!", is_error=True)
            return
        baud_rate = self.root.ids.home_scr.ids.baud_rate.text
        baud_int = self.req_template["baudrate"]
        if len(baud_rate) >= 4:
            try:
                baud_int = int(baud_rate)
                self.req_template["baudrate"] = baud_int
            finally:
                print(f"Using baud rate: {baud_int}")
        Thread(target=self.write_request, daemon=True).start()

    ## Settings sections
    def save_settings(self, instance=None, control_txt="none"):
        boot = None
        if control_txt == "switch":
            boot = not self.config_template["boot"]
        form = self.read_settings_form(boot=boot)
        if form is None:
            return
        if not self.persist_settings_form(form):
            return
        print(
            f"Service on start: {self.config_template['boot']}, "
            f"port: {self.config_template['port']}"
        )
        Clock.schedule_once(lambda dt: self.show_toast_msg("Settings has been saved."))

    def read_settings_form(self, boot=None):
        """Return the form-owned writable subset, or None when it is invalid.

        Only fields the operator actually edited are carried; everything else
        is filled from a fresh read when the form is persisted, so a remote
        change to a field left alone on this screen survives the save.
        """
        settings = self.root.ids.settings_scr.ids
        form = {}
        port_text = (settings.port_no_inp.text or "").strip()
        if port_text != self.port_loaded:
            try:
                form["port"] = int(port_text)
            except ValueError:
                self.show_toast_msg(
                    "Port must be an integer between "
                    f"{settingsSvc.PORT_MIN} and {settingsSvc.PORT_MAX}",
                    is_error=True,
                )
                return None
        boot_value = self.config_template["boot"] if boot is None else boot
        if boot_value != self.boot_loaded:
            form["boot"] = boot_value
        webhook = self.read_webhook_form()
        if webhook is None:
            return None
        form["webhook"] = webhook
        return form

    def read_webhook_inputs(self):
        settings = self.root.ids.settings_scr.ids
        return (
            bool(settings.webhook_sw.active),
            settings.webhook_url_inp.text.strip(),
            settings.webhook_secret_inp.text.strip(),
        )

    def read_webhook_form(self):
        """Return the Webhook fields the operator edited, or None when invalid.

        The secret and target follow the same rule as the port: an untouched
        field is omitted, so a change made over the network is preserved.
        """
        enabled, url, secret = self.read_webhook_inputs()
        if not self.validate_webhook(enabled, url):
            return None
        webhook = {}
        if enabled != self.webhook_enabled_loaded:
            webhook["enabled"] = enabled
        if url != self.webhook_url_loaded:
            webhook["url"] = url
        if secret != self.webhook_secret_loaded:
            webhook["secret"] = secret
        return webhook

    def persist_settings_form(self, form):
        """Merge the form over fresh Settings and persist it atomically."""
        try:
            saved = settingsSvc.save_local_settings(self.app_config, form)
        except settingsSvc.SettingsValidationError as exc:
            self.show_toast_msg(str(exc), is_error=True)
            return False
        self.apply_persisted_settings(saved)
        return True

    def apply_persisted_settings(self, settings: dict):
        """Adopt freshly persisted Settings and show them."""
        self.config_template = dict(settings)
        self.req_template["port"] = int(
            self.config_template.get("port") or settingsSvc.DEFAULT_PORT
        )
        self.refresh_settings_view()

    def webhook_switch_active(self, instance, value):
        """Apply the toggle immediately, instead of waiting for Save.

        on_active carries the new value; reading `active` from on_release is
        one event too early and saved the previous state.
        """
        if self.webhook_switch_guard:
            return
        webhook = self.read_webhook_form()
        if webhook is None:
            self.webhook_switch_guard = True
            instance.active = False
            self.webhook_switch_guard = False
            return
        if not self.persist_settings_form({"webhook": webhook}):
            self.refresh_webhook_view()
            return
        self.show_toast_msg(
            "Webhook enabled" if bool(value) else "Webhook disabled"
        )

    def validate_webhook(self, enabled: bool, url: str) -> bool:
        # Turning it off must always be possible, even with a stale URL.
        if not enabled:
            return True
        if not is_valid_target(url):
            self.show_toast_msg(
                "Add a valid http:// or https:// webhook URL first",
                is_error=True,
            )
            return False
        return True

    def refresh_webhook_view(self):
        webhook = normalize_webhook_config(
            self.config_template.get("webhook")
        )
        settings = self.root.ids.settings_scr.ids
        self.webhook_switch_guard = True
        try:
            settings.webhook_sw.active = webhook["enabled"]
        finally:
            self.webhook_switch_guard = False
        settings.webhook_url_inp.text = webhook["url"]
        settings.webhook_secret_inp.text = webhook["secret"]
        self.webhook_enabled_loaded = webhook["enabled"]
        self.webhook_url_loaded = webhook["url"].strip()
        self.webhook_secret_loaded = webhook["secret"].strip()
        self.refresh_webhook_status()

    def reload_settings_config(self):
        """Re-read persisted Settings (e.g. after an external change)."""
        if not self.app_config or not os.path.exists(self.app_config):
            return
        self.apply_persisted_settings(
            settingsSvc.load_settings(self.app_config)
        )

    def refresh_webhook_status(self):
        if not self.webhook_status_path:
            return
        status = read_webhook_status(self.webhook_status_path)
        self.root.ids.settings_scr.ids.webhook_status_lbl.text = (
            format_webhook_status(status)
        )

    def send_webhook_test(self, instance=None):
        webhook_enabled, webhook_url, webhook_secret = self.read_webhook_inputs()
        # A test always needs a URL, even while the toggle is off.
        if not self.validate_webhook(True, webhook_url):
            return
        self.root.ids.settings_scr.ids.webhook_status_lbl.text = (
            "Sending test webhook..."
        )
        config = {"url": webhook_url, "secret": webhook_secret}
        Thread(
            target=self.run_webhook_test,
            kwargs={"config": config},
            daemon=True,
        ).start()

    def run_webhook_test(self, config: dict):
        ok, status, error = send_test_webhook(config)
        Clock.schedule_once(
            lambda dt: self.show_webhook_test_result(ok, status, error)
        )

    def show_webhook_test_result(self, ok: bool, status, error):
        if ok:
            text = f"Test delivered (HTTP {status})"
        else:
            text = f"Test failed: {error or status}"
        self.root.ids.settings_scr.ids.webhook_status_lbl.text = text
        self.show_toast_msg(text, is_error=not ok)

    def refresh_settings_view(self):
        auto_start_sw = self.root.ids.settings_scr.ids.auto_start_sw
        port_no_inp = self.root.ids.settings_scr.ids.port_no_inp
        self.boot_loaded = bool(self.config_template["boot"])
        auto_start_sw.active = self.boot_loaded
        port_no_inp.text = str(self.config_template["port"])
        self.port_loaded = port_no_inp.text.strip()
        self.refresh_webhook_view()

    def reset_settings(self, instance=None):
        defaults = settingsSvc.default_settings()
        default_webhook = defaults["webhook"]
        form = {
            "port": defaults["port"],
            "boot": defaults["boot"],
            "webhook": {
                "enabled": default_webhook["enabled"],
                "url": default_webhook["url"],
                "secret": default_webhook["secret"],
                "timeout": default_webhook["timeout"],
                "max_attempts": default_webhook["max_attempts"],
                "backoff": list(default_webhook["backoff"]),
                "capacity": default_webhook["capacity"],
            },
        }
        if not self.persist_settings_form(form):
            return
        print("Settings reset has been done!")
        Clock.schedule_once(lambda dt: self.show_toast_msg("Settings reset has been done!"))

    ## App or UI related things
    def acquire_wakelock(self):
        if self.wake_lock:
            return  # already acquired
        try:
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            Context = autoclass("android.content.Context")
            activity = PythonActivity.mActivity
            PowerManager = autoclass("android.os.PowerManager")
            power_manager = cast(PowerManager, activity.getSystemService(Context.POWER_SERVICE))
            # Create wakelock (use PowerManager.FULL_WAKE_LOCK for full wakelock)
            self.wake_lock = power_manager.newWakeLock(
                PowerManager.FULL_WAKE_LOCK, "OpenSmsApp::WakeLockTag"
            )
            self.wake_lock.acquire()
            print("WakeLock acquired")
        except Exception as e:
            print(f"Wake lock aquire error: {e}")

    def release_wakelock(self):
        if self.wake_lock and self.wake_lock.isHeld():
            self.wake_lock.release()
            self.wake_lock = None
            print("WakeLock released")

    def show_toast_msg(self, message, is_error=False, duration=3):
        from kivymd.uix.snackbar import MDSnackbar, MDSnackbarText, MDSnackbarSupportingText
        bg_color = (0.2, 0.6, 0.2, 1) if not is_error else (0.8, 0.2, 0.2, 1)
        MDSnackbar(
            MDSnackbarText(
                text=message,
            ),
            y=dp(24),
            orientation="horizontal",
            pos_hint={"center_x": 0.5},
            background_color=bg_color,
        ).open()

    def show_text_dialog(
            self, 
            title, 
            text="", 
            custom_content:MDDialogContentContainer|None = None, 
            buttons:MDDialogButtonContainer|None = None
        ):
        self.txt_dialog = MDDialog(
            MDDialogHeadlineText(
                text = title
            ),
            MDDialogSupportingText(
                text = text
            ),
            custom_content,
            buttons
        )
        self.txt_dialog.open()

    def txt_dialog_closer(self, instance=None):
        if self.txt_dialog:
            self.txt_dialog.dismiss()

    def goto_app_settings(self, instance=None):
        self.txt_dialog_closer()
        self.go_to_screen("Settings")

    def goto_logs_screen(self, instance=None):
        self.root.ids.nav_drawer.set_state("close")
        self.go_to_screen("Logs")

    def go_to_screen(self, screen:str):
        self.root.ids.screen_manager.current = screen

    def on_main_screen_entry(self, instance=None):
        self.root.ids.top_back_btn.opacity = 0
        self.root.ids.top_back_btn.disabled = True

    def on_settings_entry(self, instance=None):
        self.root.ids.top_back_btn.opacity = 1
        self.root.ids.top_back_btn.disabled = False
        self.reload_settings_config()

    def back_button_action(self, instance=None):
        if self.root.ids.screen_manager.current in ["Settings", "Logs"]:
           self.go_to_screen("SMS")

    def events(self, instance, keyboard, keycode, text, modifiers):
        """Handle mobile device button presses (e.g., Android back button)."""
        if keyboard in (1001, 27):
            self.back_button_action()
            return True
        # exits from app
        return False

    def stop_resp_loop(self):
        if self.resp_thred:
            self.resp_thred.join(timeout=5)
        self.resp_thred = None

    def on_pause(self):
        self.stop_resp_loop()
        return True

    def on_resume(self):
        self.stop_resp_loop()
        self.resp_thred = Thread(target=self.resp_check_loop, daemon=True)
        self.resp_thred.start()

    # run on app exit
    def on_stop(self):
        print("Exiting the app...")
        if platform == "android":
            #self.stop_service()
            self.release_wakelock()

    # on bottom navigation
    def on_switch_tabs(
            self,
            bar: MDNavigationBar,
            item: MDNavigationItem,
            item_icon: str,
            item_text: str,
        ):
            self.root.ids.screen_manager.current = item_text


if __name__ == '__main__':
    OpenSmsApp().run()
