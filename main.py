import os, time, threading, struct
from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.spinner import Spinner
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.clock import Clock
from kivy.utils import get_color_from_hex as hex
from kivy.graphics import Color, Rectangle
from PIL import Image as PILImage
import io, base64

Window.clearcolor = hex("0a0c0f")
BG      = hex("0a0c0f")
SURFACE = hex("111418")
CARD    = hex("161b22")
ACCENT  = hex("00e5a0")
MUTED   = hex("556070")
TEXT    = hex("d4dce8")
DANGER  = hex("e05050")
WARN    = hex("f0a500")

try:
    import RNS
    import LXMF
    RNS_AVAILABLE = True
except ImportError:
    RNS_AVAILABLE = False

try:
    from jnius import autoclass
    ANDROID_AVAILABLE = True
except ImportError:
    ANDROID_AVAILABLE = False


def request_bt_permissions(callback):
    try:
        from android.permissions import request_permissions, Permission, check_permission
        perms = [
            Permission.BLUETOOTH,
            Permission.BLUETOOTH_ADMIN,
            Permission.BLUETOOTH_CONNECT,
            Permission.BLUETOOTH_SCAN,
        ]
        def on_result(permissions, grants):
            all_granted = all(grants)
            print("[BT] Permissions granted: " + str(all_granted))
            callback(all_granted)
        request_permissions(perms, on_result)
    except Exception as e:
        print("[BT] Permission request error: " + str(e))
        callback(False)


def connect_rnode(rnode_name, rnode_mac):
    if not ANDROID_AVAILABLE:
        return None, "Not on Android"
    try:
        BluetoothAdapter = autoclass('android.bluetooth.BluetoothAdapter')
        UUID = autoclass('java.util.UUID')
        SPP_UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
        adapter = BluetoothAdapter.getDefaultAdapter()

        if adapter is None:
            return None, "No BT adapter"
        if not adapter.isEnabled():
            return None, "BT is off - enable Bluetooth and retry"

        device = None

        # Try by MAC first (most reliable)
        if rnode_mac:
            try:
                device = adapter.getRemoteDevice(rnode_mac.upper())
                print("[BT] Got device by MAC: " + rnode_mac)
            except Exception as e:
                print("[BT] MAC lookup failed: " + str(e))

        # Fallback: search paired devices by name
        if device is None:
            paired = adapter.getBondedDevices().toArray()
            print("[BT] Scanning " + str(len(paired)) + " paired devices")
            for d in paired:
                name = str(d.getName())
                print("[BT] Paired: " + name)
                if rnode_name.lower() in name.lower():
                    device = d
                    print("[BT] Matched by name: " + name)
                    break

        if device is None:
            return None, "RNode not found in paired devices"

        adapter.cancelDiscovery()
        # Try reflection method first (more reliable on Android 10+)
        try:
            Method = autoclass('java.lang.reflect.Method')
            m = device.getClass().getMethod(
                "createRfcommSocket", [autoclass('java.lang.Integer').TYPE])
            socket = m.invoke(device, [1])
            print("[BT] Using reflection socket on channel 1")
        except Exception as re:
            print("[BT] Reflection failed, trying standard: " + str(re))
            socket = device.createRfcommSocketToServiceRecord(SPP_UUID)
        socket.connect()
    except Exception as e:
        msg = str(e)
        print("[BT] connect error: " + msg)
        return None, msg


def send_kiss_config(socket, freq_mhz, bw_khz, sf, cr, tx_power):
    if socket is None:
        return
    try:
        out = socket.getOutputStream()
        FEND = 0xC0
        def kiss(cmd, data):
            out.write(bytes([FEND, cmd]) + data + bytes([FEND]))
        freq_hz = int(float(freq_mhz) * 1000000)
        kiss(0x01, struct.pack(">I", freq_hz))
        kiss(0x02, struct.pack(">I", int(bw_khz) * 1000))
        kiss(0x03, bytes([int(sf)]))
        kiss(0x04, bytes([int(cr)]))
        kiss(0x05, bytes([int(tx_power)]))
        kiss(0x06, bytes([0x01]))
        out.flush()
        print("[BT] KISS config sent")
    except Exception as e:
        print("[BT] KISS error: " + str(e))


class NanobandCore:
    def __init__(self):
        self.reticulum   = None
        self.router      = None
        self.identity    = None
        self.destination = None
        self.bt_socket   = None
        self.ready       = False
        self.messages    = {}
        self.contacts    = {}

    def write_rns_config(self, config_path, bt_address, freq, bw, sf, cr, tx_power):
        os.makedirs(config_path, exist_ok=True)
        freq_hz = int(float(freq) * 1000000)
        bw_hz   = int(bw) * 1000
        cfg = (
            "[reticulum]\n"
            "  enable_transport = False\n"
            "  share_instance = Yes\n\n"
            "[interface:RNodeBT]\n"
            "  type = RNodeInterface\n"
            "  interface_enabled = True\n"
            "  port = " + bt_address + "\n"
            "  frequency = " + str(freq_hz) + "\n"
            "  bandwidth = " + str(bw_hz) + "\n"
            "  txpower = " + str(tx_power) + "\n"
            "  spreadingfactor = " + str(sf) + "\n"
            "  codingrate = " + str(cr) + "\n"
        )
        with open(os.path.join(config_path, "config"), "w") as f:
            f.write(cfg)

    def start_rns(self, config_path, display_name,
                  bt_socket, bt_address, freq, bw, sf, cr, tx_power):
        try:
            if bt_socket:
                send_kiss_config(bt_socket, freq, bw, sf, cr, tx_power)
                rns_path = os.path.join(config_path, "rns")
                self.write_rns_config(rns_path, bt_address,
                                      freq, bw, sf, cr, tx_power)
            else:
                rns_path = config_path

            self.reticulum = RNS.Reticulum(rns_path)
            id_path = os.path.join(config_path, "identity")
            if os.path.exists(id_path):
                self.identity = RNS.Identity.from_file(id_path)
            else:
                self.identity = RNS.Identity()
                self.identity.to_file(id_path)
            self.router = LXMF.LXMRouter(
                storagepath=config_path, identity=self.identity)
            self.destination = self.router.register_delivery_identity(
                self.identity, display_name=display_name)
            self.router.register_delivery_callback(self._on_receive)
            self.bt_socket = bt_socket
            self.ready = True
            print("[CORE] Ready")
            return True
        except Exception as e:
            print("[CORE] start_rns error: " + str(e))
            return False

    def _on_receive(self, message):
        sender  = RNS.prettyhexrep(message.source_hash)
        content = message.content.decode("utf-8", errors="replace")
        if sender not in self.messages:
            self.messages[sender] = []
        self.messages[sender].append({
            "from": sender, "txt": content, "img": None, "ts": time.time()
        })

    def send(self, dest_hash_str, text):
        if not self.ready:
            return False
        try:
            clean = dest_hash_str.replace("<","").replace(">","").replace(":","")
            dest_hash = bytes.fromhex(clean)
            if not RNS.Transport.has_path(dest_hash):
                RNS.Transport.request_path(dest_hash)
                t = 10
                while not RNS.Transport.has_path(dest_hash) and t > 0:
                    time.sleep(0.5); t -= 0.5
            id_dest = RNS.Identity.recall(dest_hash)
            if id_dest is None:
                return False
            lxmf_dest = RNS.Destination(
                id_dest, RNS.Destination.OUT,
                RNS.Destination.SINGLE, "lxmf", "delivery")
            msg = LXMF.LXMessage(
                lxmf_dest, self.destination,
                text.encode("utf-8"),
                desired_method=LXMF.LXMessage.DIRECT)
            self.router.handle_outbound(msg)
            return True
        except Exception as e:
            print("[CORE] send error: " + str(e))
            return False

    @property
    def my_hash(self):
        if self.identity and self.destination:
            return RNS.prettyhexrep(self.destination.hash)
        return "not started"


# UI helpers
def sbtn(text, bg=ACCENT, fg=BG, h=44):
    return Button(text=text, size_hint_y=None, height=dp(h),
                  background_normal="", background_color=bg,
                  color=fg, font_size=dp(13), bold=True)

def sinput(hint="", h=40):
    return TextInput(hint_text=hint, multiline=False,
                     size_hint_y=None, height=dp(h),
                     background_color=CARD, foreground_color=TEXT,
                     hint_text_color=MUTED, cursor_color=ACCENT,
                     font_size=dp(13), padding=[dp(10), dp(10)])

def lbl(text, size=13, color=TEXT, bold=False):
    return Label(text=text, font_size=dp(size), color=color, bold=bold,
                 size_hint_y=None, height=dp(size * 1.8),
                 halign="left", text_size=(None, None))

def compress_image(path, max_px=160, quality=55):
    try:
        img = PILImage.open(path).convert("RGB")
        r = min(max_px / img.width, max_px / img.height)
        if r < 1:
            img = img.resize((int(img.width*r), int(img.height*r)), PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print("[IMG] " + str(e))
        return None


class MessagesScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="messages", **kw)
        self.app_ref = app_ref
        layout = BoxLayout(orientation="vertical")
        hdr = BoxLayout(size_hint_y=None, height=dp(44), padding=[dp(14), 0])
        with hdr.canvas.before:
            Color(*SURFACE); Rectangle(pos=hdr.pos, size=hdr.size)
        hdr.add_widget(lbl("MESSAGES", size=12, color=MUTED, bold=True))
        layout.add_widget(hdr)
        self.scroll = ScrollView()
        self.list = BoxLayout(orientation="vertical", size_hint_y=None,
                              spacing=1, padding=[0, dp(4)])
        self.list.bind(minimum_height=self.list.setter("height"))
        self.scroll.add_widget(self.list)
        layout.add_widget(self.scroll)
        self.add_widget(layout)
        self.refresh()

    def refresh(self):
        self.list.clear_widgets()
        contacts = self.app_ref.core.contacts
        messages = self.app_ref.core.messages
        if not messages:
            self.list.add_widget(lbl(
                "No conversations. Go to Contacts to start.", color=MUTED, size=12))
            return
        for h, msgs in messages.items():
            if not msgs: continue
            last  = msgs[-1]
            name  = contacts.get(h, h[:16])
            preview = last.get("txt", "[image]")[:40]
            btn = Button(
                text="[b]" + name + "[/b]\n" + preview,
                markup=True, size_hint_y=None, height=dp(60),
                background_normal="", background_color=SURFACE,
                color=TEXT, font_size=dp(13), halign="left",
                text_size=(Window.width - dp(28), None))
            btn.bind(on_press=lambda x, h=h, n=name: self.app_ref.open_chat(h, n))
            self.list.add_widget(btn)


class ContactsScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="contacts", **kw)
        self.app_ref = app_ref
        layout = BoxLayout(orientation="vertical", padding=[dp(14), dp(8)], spacing=dp(6))
        layout.add_widget(lbl("CONTACTS", size=12, color=MUTED, bold=True))
        self.scroll = ScrollView()
        self.list = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        self.list.bind(minimum_height=self.list.setter("height"))
        self.scroll.add_widget(self.list)
        layout.add_widget(self.scroll)
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.name_in = sinput("Display name")
        self.hash_in = sinput("Hash e.g. a1b2c3...")
        add_btn = sbtn("ADD")
        add_btn.bind(on_press=self.add_contact)
        row.add_widget(self.name_in)
        row.add_widget(self.hash_in)
        row.add_widget(add_btn)
        layout.add_widget(row)
        self.add_widget(layout)
        self.refresh()

    def add_contact(self, *a):
        name = self.name_in.text.strip()
        h = self.hash_in.text.strip().lower().replace(" ", "")
        if name and h:
            self.app_ref.core.contacts[h] = name
            self.name_in.text = ""
            self.hash_in.text = ""
            self.refresh()

    def refresh(self):
        self.list.clear_widgets()
        for h, name in self.app_ref.core.contacts.items():
            btn = Button(
                text="[b]" + name + "[/b]  [color=556070]" + h[:20] + "...[/color]",
                markup=True, size_hint_y=None, height=dp(52),
                background_normal="", background_color=CARD, color=TEXT, font_size=dp(13))
            btn.bind(on_press=lambda x, h=h, n=name: (
                self.app_ref.open_chat(h, n),
                setattr(self.app_ref.sm, "current", "messages")))
            self.list.add_widget(btn)


class ChatScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="chat", **kw)
        self.app_ref = app_ref
        self.dest_hash = None
        layout = BoxLayout(orientation="vertical")
        hdr = BoxLayout(size_hint_y=None, height=dp(48), padding=[dp(8), 0], spacing=dp(8))
        with hdr.canvas.before:
            Color(*SURFACE); Rectangle(pos=hdr.pos, size=hdr.size)
        back = sbtn("BACK", bg=SURFACE, fg=ACCENT, h=44)
        back.size_hint_x = None; back.width = dp(60)
        back.bind(on_press=lambda x: setattr(self.app_ref.sm, "current", "messages"))
        self.title_lbl = lbl("", size=14, bold=True)
        hdr.add_widget(back); hdr.add_widget(self.title_lbl)
        layout.add_widget(hdr)
        self.scroll = ScrollView()
        self.msg_list = BoxLayout(orientation="vertical", size_hint_y=None,
                                  spacing=dp(6), padding=[dp(12), dp(8)])
        self.msg_list.bind(minimum_height=self.msg_list.setter("height"))
        self.scroll.add_widget(self.msg_list)
        layout.add_widget(self.scroll)
        inp = BoxLayout(size_hint_y=None, height=dp(52), padding=[dp(8), dp(6)], spacing=dp(6))
        with inp.canvas.before:
            Color(*SURFACE); Rectangle(pos=inp.pos, size=inp.size)
        self.text_in = sinput("Type message...", h=40)
        self.text_in.size_hint_x = 0.7
        cam_btn = sbtn("CAM", bg=CARD, fg=TEXT, h=40)
        cam_btn.size_hint_x = None; cam_btn.width = dp(50)
        send_btn = sbtn("SEND", h=40)
        send_btn.size_hint_x = None; send_btn.width = dp(60)
        cam_btn.bind(on_press=self.pick_image)
        send_btn.bind(on_press=self.send_msg)
        inp.add_widget(self.text_in); inp.add_widget(cam_btn); inp.add_widget(send_btn)
        layout.add_widget(inp)
        self.add_widget(layout)

    def load(self, dest_hash, dest_name):
        self.dest_hash = dest_hash
        self.title_lbl.text = dest_name
        self.refresh()

    def refresh(self):
        self.msg_list.clear_widgets()
        msgs = self.app_ref.core.messages.get(self.dest_hash, [])
        for m in msgs:
            is_me = m["from"] == "me"
            row = BoxLayout(size_hint_y=None, height=dp(48))
            if is_me: row.add_widget(Label(size_hint_x=0.25))
            bubble = Button(
                text=m.get("txt", "[image]"),
                size_hint_x=0.75, size_hint_y=None, height=dp(48),
                background_normal="",
                background_color=hex("00b87a33") if is_me else CARD,
                color=TEXT, font_size=dp(13),
                halign="right" if is_me else "left",
                text_size=(Window.width * 0.65, None))
            row.add_widget(bubble)
            if not is_me: row.add_widget(Label(size_hint_x=0.25))
            self.msg_list.add_widget(row)
        Clock.schedule_once(lambda dt: setattr(self.scroll, "scroll_y", 0), 0.1)

    def send_msg(self, *a):
        txt = self.text_in.text.strip()
        if not txt: return
        core = self.app_ref.core
        if self.dest_hash not in core.messages:
            core.messages[self.dest_hash] = []
        core.messages[self.dest_hash].append({
            "from": "me", "txt": txt, "img": None, "ts": time.time()})
        threading.Thread(target=core.send, args=(self.dest_hash, txt), daemon=True).start()
        self.text_in.text = ""
        self.refresh()

    def pick_image(self, *a):
        try:
            from plyer import camera
            camera.take_picture(
                filename=os.path.join(App.get_running_app().user_data_dir, "capture.jpg"),
                on_complete=self.on_image_captured)
        except Exception as e:
            print("[CAM] " + str(e))

    def on_image_captured(self, path):
        if not path or not os.path.exists(path): return
        s = self.app_ref.settings
        b64 = compress_image(path, max_px=s.get("img_max_px", 160), quality=s.get("img_quality", 55))
        if not b64: return
        core = self.app_ref.core
        if self.dest_hash not in core.messages:
            core.messages[self.dest_hash] = []
        core.messages[self.dest_hash].append({
            "from": "me", "txt": "[image]", "img": b64, "ts": time.time()})
        threading.Thread(target=core.send, args=(self.dest_hash, "[IMG]" + b64), daemon=True).start()
        self.refresh()


class SettingsScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="settings", **kw)
        self.app_ref = app_ref
        scroll = ScrollView()
        layout = BoxLayout(orientation="vertical", size_hint_y=None,
                           spacing=dp(4), padding=[dp(14), dp(8)])
        layout.bind(minimum_height=layout.setter("height"))

        def section(t): layout.add_widget(lbl(t, size=10, color=ACCENT, bold=True))
        def row(label, widget):
            r = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            r.add_widget(lbl(label, size=13)); r.add_widget(widget)
            layout.add_widget(r)

        s = app_ref.settings
        section("RNODE - BLUETOOTH")
        self.rnode_name = sinput("e.g. Rnode 7AEB7")
        self.rnode_name.text = s.get("rnode_name", "")
        row("RNode name", self.rnode_name)
        self.rnode_mac = sinput("e.g. F0:24:F9:8D:AF:66")
        self.rnode_mac.text = s.get("rnode_mac", "")
        row("RNode MAC", self.rnode_mac)

        section("RADIO PARAMETERS")
        self.freq = sinput("e.g. 868.125")
        self.freq.text = s.get("freq", "868.125")
        row("Frequency (MHz)", self.freq)
        self.bw = Spinner(text=s.get("bw","125"), values=["125","250","500"],
                          size_hint_y=None, height=dp(40), background_color=CARD, color=TEXT)
        row("Bandwidth (kHz)", self.bw)
        self.sf = Spinner(text=s.get("sf","9"), values=["7","8","9","10","11","12"],
                          size_hint_y=None, height=dp(40), background_color=CARD, color=TEXT)
        row("Spreading Factor", self.sf)
        self.cr = Spinner(text=s.get("cr","6"), values=["5","6","7","8"],
                          size_hint_y=None, height=dp(40), background_color=CARD, color=TEXT)
        row("Coding Rate", self.cr)
        self.txp = sinput("1-22")
        self.txp.text = str(s.get("tx_power", 14))
        row("TX Power (dBm)", self.txp)

        section("IMAGE TRANSFER")
        self.img_px = sinput("pixels")
        self.img_px.text = str(s.get("img_max_px", 160))
        row("Max width (px)", self.img_px)
        self.img_q = sinput("20-80")
        self.img_q.text = str(s.get("img_quality", 55))
        row("JPEG quality (%)", self.img_q)

        section("IDENTITY")
        self.disp = sinput("Your name")
        self.disp.text = s.get("display_name", "Field Unit")
        row("Display name", self.disp)
        layout.add_widget(lbl("Hash: " + app_ref.core.my_hash, size=11, color=ACCENT))

        section("RETICULUM")
        self.prop = sinput("Propagation node hash")
        self.prop.text = s.get("prop_node_hash", "")
        row("Prop node hash", self.prop)

        save_btn = sbtn("SAVE SETTINGS")
        save_btn.bind(on_press=self.save)
        layout.add_widget(save_btn)
        layout.add_widget(lbl(
            "NANOBAND v0.1 - LXMF/RNS - Text + low-res image only",
            size=10, color=MUTED))

        scroll.add_widget(layout)
        self.add_widget(scroll)

    def save(self, *a):
        s = self.app_ref.settings
        s["rnode_name"]     = self.rnode_name.text.strip()
        s["rnode_mac"]      = self.rnode_mac.text.strip()
        s["freq"]           = self.freq.text.strip()
        s["bw"]             = self.bw.text
        s["sf"]             = self.sf.text
        s["cr"]             = self.cr.text
        s["tx_power"]       = int(self.txp.text.strip() or 14)
        s["img_max_px"]     = int(self.img_px.text.strip() or 160)
        s["img_quality"]    = int(self.img_q.text.strip() or 55)
        s["display_name"]   = self.disp.text.strip()
        s["prop_node_hash"] = self.prop.text.strip()
        print("[SETTINGS] Saved")


class NanobandApp(App):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.core = NanobandCore()
        self.settings = {
            "rnode_name":     "Rnode 7AEB7",
            "rnode_mac":      "F0:24:F9:8D:AF:66",
            "freq":           "868.125",
            "bw":             "125",
            "sf":             "9",
            "cr":             "6",
            "tx_power":       14,
            "img_max_px":     160,
            "img_quality":    55,
            "display_name":   "Field Unit",
            "prop_node_hash": "",
        }

    def build(self):
        self.sm = ScreenManager()
        self.sm.add_widget(MessagesScreen(self))
        self.sm.add_widget(ContactsScreen(self))
        self.sm.add_widget(ChatScreen(self))
        self.sm.add_widget(SettingsScreen(self))

        root = BoxLayout(orientation="vertical")

        # Status bar
        self.status_lbl = Label(
            text="NANOBAND | tap Connect to link RNode",
            size_hint_y=None, height=dp(22),
            font_size=dp(10), color=MUTED, halign="center")
        root.add_widget(self.status_lbl)

        # Connect button bar
        self.connect_bar = BoxLayout(size_hint_y=None, height=dp(40), padding=[dp(8), dp(4)])
        self.connect_btn = sbtn("CONNECT RNODE", bg=ACCENT, fg=BG, h=32)
        self.connect_btn.bind(on_press=self.do_connect)
        self.connect_bar.add_widget(self.connect_btn)
        root.add_widget(self.connect_bar)

        root.add_widget(self.sm)

        # Bottom nav
        nav = BoxLayout(size_hint_y=None, height=dp(52))
        with nav.canvas.before:
            Color(*SURFACE); Rectangle(pos=nav.pos, size=nav.size)
        for name, icon in [("messages","Msgs"), ("contacts","Contacts"), ("settings","Settings")]:
            b = Button(text=icon, background_normal="",
                       background_color=SURFACE, color=MUTED, font_size=dp(12))
            b.bind(on_press=lambda x, n=name: setattr(self.sm, "current", n))
            nav.add_widget(b)
        root.add_widget(nav)

        # Start RNS without radio first so app is usable
        config_dir = os.path.join(self.user_data_dir, "nanoband")
        os.makedirs(config_dir, exist_ok=True)
        self.config_dir = config_dir
        threading.Thread(target=self._init_rns, args=(config_dir,), daemon=True).start()

        return root

    def _init_rns(self, config_dir):
        if not RNS_AVAILABLE:
            Clock.schedule_once(lambda dt: setattr(
                self.status_lbl, "text", "NANOBAND | RNS not available"), 0)
            return
        try:
            self.core.reticulum = RNS.Reticulum(config_dir)
            id_path = os.path.join(config_dir, "identity")
            if os.path.exists(id_path):
                self.core.identity = RNS.Identity.from_file(id_path)
            else:
                self.core.identity = RNS.Identity()
                self.core.identity.to_file(id_path)
            self.core.router = LXMF.LXMRouter(
                storagepath=config_dir, identity=self.core.identity)
            self.core.destination = self.core.router.register_delivery_identity(
                self.core.identity, display_name=self.settings["display_name"])
            self.core.router.register_delivery_callback(self.core._on_receive)
            self.core.ready = True
            Clock.schedule_once(lambda dt: setattr(
                self.status_lbl, "text",
                "NANOBAND | RNS ready | tap Connect"), 0)
        except Exception as e:
            print("[APP] RNS init error: " + str(e))

    def do_connect(self, *a):
        s = self.settings
        self.connect_btn.text = "Requesting permissions..."
        self.connect_btn.background_color = WARN

        def after_permissions(granted):
            if not granted:
                Clock.schedule_once(lambda dt: self._set_connect_status(
                    "Permission denied - check app BT permissions", DANGER), 0)
                return
            Clock.schedule_once(lambda dt: self._set_connect_status(
                "Connecting...", WARN), 0)
            threading.Thread(target=self._do_bt_connect, daemon=True).start()

        request_bt_permissions(after_permissions)

    def _do_bt_connect(self):
        s = self.settings
        socket, msg = connect_rnode(s["rnode_name"], s["rnode_mac"])

        if socket is None:
            Clock.schedule_once(lambda dt: self._set_connect_status(
                "BT FAILED: " + msg, DANGER), 0)
            return

        # Reconnect RNS with BT interface
        try:
            if self.core.reticulum:
                pass  # already running, just add interface

            bt_address = s["rnode_mac"]
            send_kiss_config(socket, s["freq"], s["bw"],
                             s["sf"], s["cr"], s["tx_power"])
            rns_path = os.path.join(self.config_dir, "rns")
            self.core.write_rns_config(
                rns_path, bt_address,
                s["freq"], s["bw"], s["sf"], s["cr"], s["tx_power"])
            self.core.bt_socket = socket

            Clock.schedule_once(lambda dt: self._set_connect_status(
                "BT LINKED | " + self.core.my_hash[:16], ACCENT), 0)
        except Exception as e:
            Clock.schedule_once(lambda dt: self._set_connect_status(
                "RNS error: " + str(e), DANGER), 0)

    def _set_connect_status(self, text, color):
        self.status_lbl.text = "NANOBAND | " + text
        self.status_lbl.color = color
        if color == ACCENT:
            self.connect_btn.text = "RECONNECT RNODE"
            self.connect_btn.background_color = CARD
        elif color == DANGER:
            self.connect_btn.text = "RETRY CONNECT"
            self.connect_btn.background_color = DANGER
        else:
            self.connect_btn.text = "CONNECTING..."

    def open_chat(self, dest_hash, dest_name):
        chat = self.sm.get_screen("chat")
        chat.load(dest_hash, dest_name)
        self.sm.current = "chat"


if __name__ == "__main__":
    NanobandApp().run()