import os, time, threading, struct, queue
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

# RNS / LXMF
try:
    import RNS
    import LXMF
    RNS_AVAILABLE = True
except ImportError:
    RNS_AVAILABLE = False

# able BLE library - only on Android
try:
    from able import BluetoothDispatcher, GATT_SUCCESS
    ABLE_AVAILABLE = True
except ImportError:
    ABLE_AVAILABLE = False

# NUS UUIDs (Nordic UART Service - used by RNode firmware over BLE)
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write to RNode
NUS_TX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notifications from RNode

# KISS framing constants
KISS_FEND  = 0xC0
KISS_FESC  = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD

# KISS commands
CMD_DATA        = 0x00
CMD_FREQUENCY   = 0x01
CMD_BANDWIDTH   = 0x02
CMD_SF          = 0x03
CMD_CR          = 0x04
CMD_TXPOWER     = 0x05
CMD_RADIO_STATE = 0x06


def kiss_escape(data):
    out = bytearray()
    for b in data:
        if b == KISS_FEND:
            out += bytes([KISS_FESC, KISS_TFEND])
        elif b == KISS_FESC:
            out += bytes([KISS_FESC, KISS_TFESC])
        else:
            out.append(b)
    return bytes(out)


def kiss_frame(cmd, data):
    return bytes([KISS_FEND, cmd]) + kiss_escape(data) + bytes([KISS_FEND])


def build_rnode_config(freq_mhz, bw_khz, sf, cr, tx_power):
    frames = bytearray()
    freq_hz = int(float(freq_mhz) * 1_000_000)
    bw_hz   = int(bw_khz) * 1000
    frames += kiss_frame(CMD_FREQUENCY,   struct.pack(">I", freq_hz))
    frames += kiss_frame(CMD_BANDWIDTH,   struct.pack(">I", bw_hz))
    frames += kiss_frame(CMD_SF,          bytes([int(sf)]))
    frames += kiss_frame(CMD_CR,          bytes([int(cr)]))
    frames += kiss_frame(CMD_TXPOWER,     bytes([int(tx_power)]))
    frames += kiss_frame(CMD_RADIO_STATE, bytes([0x01]))
    return bytes(frames)


class RNodeBLE(BluetoothDispatcher if ABLE_AVAILABLE else object):
    """BLE connection to RNode using Nordic UART Service via able library."""

    def __init__(self, mac, on_status, on_rx):
        if ABLE_AVAILABLE:
            super().__init__()
        self.mac        = mac.upper()
        self.on_status  = on_status   # callback(str, color)
        self.on_rx      = on_rx       # callback(bytes) - incoming KISS frames
        self.rx_char    = None
        self.tx_char    = None
        self.connected  = False
        self._write_q   = queue.Queue()
        self._kiss_buf  = bytearray()

    def connect(self):
        if not ABLE_AVAILABLE:
            self.on_status("BLE library not available", DANGER)
            return
        self.on_status("Connecting to " + self.mac + "...", WARN)
        self.connect_by_device_address(self.mac)

    # --- able callbacks ---

    def on_connection_state_change(self, status, state):
        if status == GATT_SUCCESS and state:
            self.on_status("BLE connected, discovering services...", WARN)
            self.discover_services()
        else:
            self.connected = False
            self.on_status("BLE disconnected (state=" + str(state) + ")", DANGER)

    def on_services(self, status, services):
        if status != GATT_SUCCESS:
            self.on_status("Service discovery failed: " + str(status), DANGER)
            return
        # find NUS RX and TX characteristics
        self.rx_char = services.search(NUS_RX_CHAR)
        self.tx_char = services.search(NUS_TX_CHAR)
        if self.rx_char is None or self.tx_char is None:
            self.on_status("NUS service not found on RNode", DANGER)
            return
        # enable notifications on TX char (data FROM rnode)
        self.enable_notifications(self.tx_char)

    def on_characteristic_changed(self, characteristic):
        # data arriving FROM the RNode
        data = bytes(characteristic.getValue())
        self._accumulate_kiss(data)

    def on_characteristic_write(self, characteristic, status):
        # previous write done - send next queued chunk if any
        if not self._write_q.empty():
            chunk = self._write_q.get_nowait()
            self._do_write(chunk)

    def on_descriptor_write(self, descriptor, status):
        # notifications enabled - we are fully ready
        self.connected = True
        self.on_status("RNode BLE ready", ACCENT)

    # --- write helpers ---

    def write_bytes(self, data):
        """Queue raw bytes for BLE write, split into 20-byte MTU chunks."""
        if not self.connected or self.rx_char is None:
            return
        chunk_size = 20
        chunks = [data[i:i+chunk_size] for i in range(0, len(data), chunk_size)]
        for chunk in chunks:
            self._write_q.put(chunk)
        # kick off first write
        if not self._write_q.empty():
            self._do_write(self._write_q.get_nowait())

    def _do_write(self, chunk):
        self.rx_char.setValue(bytearray(chunk))
        self.write_characteristic(self.rx_char)

    def send_kiss_config(self, freq, bw, sf, cr, tx_power):
        data = build_rnode_config(freq, bw, sf, cr, tx_power)
        self.write_bytes(data)

    # --- incoming KISS parser ---

    def _accumulate_kiss(self, data):
        for b in data:
            if b == KISS_FEND:
                if len(self._kiss_buf) > 1:
                    self.on_rx(bytes(self._kiss_buf))
                self._kiss_buf = bytearray()
            else:
                self._kiss_buf.append(b)


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


class NanobandCore:
    def __init__(self):
        self.reticulum   = None
        self.router      = None
        self.identity    = None
        self.destination = None
        self.ready       = False
        self.messages    = {}
        self.contacts    = {}

    def write_rns_config(self, rns_path, freq, bw, sf, cr, tx_power):
        """Write RNS config with a local TCP pipe interface.
        RNS will be given packets by our BLE bridge via this pipe."""
        os.makedirs(rns_path, exist_ok=True)
        freq_hz = int(float(freq) * 1_000_000)
        bw_hz   = int(bw) * 1000
        cfg = (
            "[reticulum]\n"
            "  enable_transport = False\n"
            "  share_instance = Yes\n\n"
            "[interfaces]\n\n"
            "  [[RNode BLE]]\n"
            "    type = RNodeInterface\n"
            "    interface_enabled = True\n"
            "    port = /dev/rfcomm0\n"
            "    frequency = " + str(freq_hz) + "\n"
            "    bandwidth = " + str(bw_hz) + "\n"
            "    txpower = " + str(tx_power) + "\n"
            "    spreadingfactor = " + str(sf) + "\n"
            "    codingrate = " + str(cr) + "\n"
        )
        with open(os.path.join(rns_path, "config"), "w") as f:
            f.write(cfg)

    def start(self, config_path, display_name):
        if not RNS_AVAILABLE:
            print("[CORE] RNS not available")
            return False
        try:
            self.reticulum = RNS.Reticulum(config_path)
            id_path = os.path.join(config_path, "identity")
            if os.path.exists(id_path):
                self.identity = RNS.Identity.from_file(id_path)
            else:
                self.identity = RNS.Identity()
                self.identity.to_file(id_path)
            self.router = LXMF.LXMRouter(
                storagepath=config_path,
                identity=self.identity
            )
            self.destination = self.router.register_delivery_identity(
                self.identity,
                display_name=display_name
            )
            self.router.register_delivery_callback(self._on_receive)
            self.ready = True
            print("[CORE] RNS/LXMF ready. Hash: " + self.my_hash)
            return True
        except Exception as e:
            print("[CORE] start error: " + str(e))
            return False

    def _on_receive(self, message):
        sender  = RNS.prettyhexrep(message.source_hash)
        content = message.content.decode("utf-8", errors="replace")
        if sender not in self.messages:
            self.messages[sender] = []
        self.messages[sender].append({
            "from": sender, "txt": content, "img": None, "ts": time.time()
        })
        print("[CORE] Message from " + sender + ": " + content[:40])

    def send(self, dest_hash_str, text):
        if not self.ready:
            return False
        try:
            clean = dest_hash_str.replace("<","").replace(">","").replace(":","")
            dest_hash = bytes.fromhex(clean)
            if not RNS.Transport.has_path(dest_hash):
                RNS.Transport.request_path(dest_hash)
                t = 15
                while not RNS.Transport.has_path(dest_hash) and t > 0:
                    time.sleep(0.5)
                    t -= 0.5
            id_dest = RNS.Identity.recall(dest_hash)
            if id_dest is None:
                print("[CORE] Identity not known for " + dest_hash_str)
                return False
            lxmf_dest = RNS.Destination(
                id_dest, RNS.Destination.OUT,
                RNS.Destination.SINGLE, "lxmf", "delivery"
            )
            msg = LXMF.LXMessage(
                lxmf_dest, self.destination,
                text.encode("utf-8"),
                desired_method=LXMF.LXMessage.DIRECT
            )
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


# --- Screens ---

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
        messages = self.app_ref.core.messages
        contacts = self.app_ref.core.contacts
        if not messages:
            self.list.add_widget(lbl(
                "No conversations yet. Add a contact to start.",
                color=MUTED, size=12))
            return
        for h, msgs in messages.items():
            if not msgs: continue
            name    = contacts.get(h, h[:16])
            preview = msgs[-1].get("txt", "[image]")[:40]
            btn = Button(
                text="[b]" + name + "[/b]\n" + preview,
                markup=True, size_hint_y=None, height=dp(60),
                background_normal="", background_color=SURFACE,
                color=TEXT, font_size=dp(13), halign="left",
                text_size=(Window.width - dp(28), None))
            btn.bind(on_press=lambda x, h=h, n=name:
                self.app_ref.open_chat(h, n))
            self.list.add_widget(btn)


class ContactsScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="contacts", **kw)
        self.app_ref = app_ref
        layout = BoxLayout(orientation="vertical",
                           padding=[dp(14), dp(8)], spacing=dp(6))
        layout.add_widget(lbl("CONTACTS", size=12, color=MUTED, bold=True))
        self.scroll = ScrollView()
        self.list = BoxLayout(orientation="vertical", size_hint_y=None,
                              spacing=dp(4))
        self.list.bind(minimum_height=self.list.setter("height"))
        self.scroll.add_widget(self.list)
        layout.add_widget(self.scroll)
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.name_in = sinput("Display name")
        self.hash_in = sinput("Destination hash")
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
        h    = self.hash_in.text.strip().lower().replace(" ", "")
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
                background_normal="", background_color=CARD,
                color=TEXT, font_size=dp(13))
            btn.bind(on_press=lambda x, h=h, n=name: (
                self.app_ref.open_chat(h, n),
                setattr(self.app_ref.sm, "current", "messages")))
            self.list.add_widget(btn)


class ChatScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="chat", **kw)
        self.app_ref   = app_ref
        self.dest_hash = None
        layout = BoxLayout(orientation="vertical")
        hdr = BoxLayout(size_hint_y=None, height=dp(48),
                        padding=[dp(8), 0], spacing=dp(8))
        with hdr.canvas.before:
            Color(*SURFACE); Rectangle(pos=hdr.pos, size=hdr.size)
        back = sbtn("BACK", bg=SURFACE, fg=ACCENT, h=44)
        back.size_hint_x = None; back.width = dp(60)
        back.bind(on_press=lambda x:
            setattr(self.app_ref.sm, "current", "messages"))
        self.title_lbl = lbl("", size=14, bold=True)
        hdr.add_widget(back); hdr.add_widget(self.title_lbl)
        layout.add_widget(hdr)
        self.scroll = ScrollView()
        self.msg_list = BoxLayout(orientation="vertical", size_hint_y=None,
                                  spacing=dp(6), padding=[dp(12), dp(8)])
        self.msg_list.bind(minimum_height=self.msg_list.setter("height"))
        self.scroll.add_widget(self.msg_list)
        layout.add_widget(self.scroll)
        inp = BoxLayout(size_hint_y=None, height=dp(52),
                        padding=[dp(8), dp(6)], spacing=dp(6))
        with inp.canvas.before:
            Color(*SURFACE); Rectangle(pos=inp.pos, size=inp.size)
        self.text_in = sinput("Type message...", h=40)
        self.text_in.size_hint_x = 0.7
        cam_btn  = sbtn("CAM",  bg=CARD, fg=TEXT, h=40)
        send_btn = sbtn("SEND", h=40)
        cam_btn.size_hint_x  = None; cam_btn.width  = dp(50)
        send_btn.size_hint_x = None; send_btn.width = dp(60)
        cam_btn.bind(on_press=self.pick_image)
        send_btn.bind(on_press=self.send_msg)
        inp.add_widget(self.text_in)
        inp.add_widget(cam_btn)
        inp.add_widget(send_btn)
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
            row   = BoxLayout(size_hint_y=None, height=dp(48))
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
        Clock.schedule_once(
            lambda dt: setattr(self.scroll, "scroll_y", 0), 0.1)

    def send_msg(self, *a):
        txt = self.text_in.text.strip()
        if not txt: return
        core = self.app_ref.core
        if self.dest_hash not in core.messages:
            core.messages[self.dest_hash] = []
        core.messages[self.dest_hash].append({
            "from": "me", "txt": txt, "img": None, "ts": time.time()})
        threading.Thread(
            target=core.send, args=(self.dest_hash, txt), daemon=True
        ).start()
        self.text_in.text = ""
        self.refresh()

    def pick_image(self, *a):
        try:
            from plyer import camera
            camera.take_picture(
                filename=os.path.join(
                    App.get_running_app().user_data_dir, "capture.jpg"),
                on_complete=self.on_image_captured)
        except Exception as e:
            print("[CAM] " + str(e))

    def on_image_captured(self, path):
        if not path or not os.path.exists(path): return
        s   = self.app_ref.settings
        b64 = compress_image(path,
                             max_px=s.get("img_max_px", 160),
                             quality=s.get("img_quality", 55))
        if not b64: return
        core = self.app_ref.core
        if self.dest_hash not in core.messages:
            core.messages[self.dest_hash] = []
        core.messages[self.dest_hash].append({
            "from": "me", "txt": "[image]", "img": b64, "ts": time.time()})
        threading.Thread(
            target=core.send,
            args=(self.dest_hash, "[IMG]" + b64),
            daemon=True
        ).start()
        self.refresh()


class SettingsScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="settings", **kw)
        self.app_ref = app_ref
        scroll = ScrollView()
        layout = BoxLayout(orientation="vertical", size_hint_y=None,
                           spacing=dp(4), padding=[dp(14), dp(8)])
        layout.bind(minimum_height=layout.setter("height"))

        def section(t):
            layout.add_widget(lbl(t, size=10, color=ACCENT, bold=True))

        def row(label, widget):
            r = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            r.add_widget(lbl(label, size=13))
            r.add_widget(widget)
            layout.add_widget(r)

        s = app_ref.settings

        section("RNODE - BLUETOOTH")
        self.rnode_mac = sinput("e.g. F0:24:F9:B4:FB:6E")
        self.rnode_mac.text = s.get("rnode_mac", "")
        row("RNode MAC address", self.rnode_mac)

        section("RADIO PARAMETERS")
        self.freq = sinput("e.g. 868.125")
        self.freq.text = s.get("freq", "868.125")
        row("Frequency (MHz)", self.freq)

        self.bw = Spinner(text=s.get("bw", "125"),
                          values=["125", "250", "500"],
                          size_hint_y=None, height=dp(40),
                          background_color=CARD, color=TEXT)
        row("Bandwidth (kHz)", self.bw)

        self.sf = Spinner(text=s.get("sf", "9"),
                          values=["7", "8", "9", "10", "11", "12"],
                          size_hint_y=None, height=dp(40),
                          background_color=CARD, color=TEXT)
        row("Spreading Factor", self.sf)

        self.cr = Spinner(text=s.get("cr", "6"),
                          values=["5", "6", "7", "8"],
                          size_hint_y=None, height=dp(40),
                          background_color=CARD, color=TEXT)
        row("Coding Rate (4/x)", self.cr)

        self.txp = sinput("1-22")
        self.txp.text = str(s.get("tx_power", 14))
        row("TX Power (dBm)", self.txp)

        section("IMAGE TRANSFER")
        self.img_px = sinput("pixels")
        self.img_px.text = str(s.get("img_max_px", 160))
        row("Max image size (px)", self.img_px)

        self.img_q = sinput("20-80")
        self.img_q.text = str(s.get("img_quality", 55))
        row("JPEG quality (%)", self.img_q)

        section("IDENTITY")
        self.disp = sinput("Your display name")
        self.disp.text = s.get("display_name", "Field Unit")
        row("Display name", self.disp)

        self.hash_lbl = lbl("Hash: " + app_ref.core.my_hash,
                            size=11, color=ACCENT)
        layout.add_widget(self.hash_lbl)

        section("RETICULUM")
        self.prop = sinput("Propagation node hash (optional)")
        self.prop.text = s.get("prop_node_hash", "")
        row("Prop node hash", self.prop)

        save_btn = sbtn("SAVE SETTINGS")
        save_btn.bind(on_press=self.save)
        layout.add_widget(save_btn)

        layout.add_widget(lbl(
            "NANOBAND v0.1  |  LXMF over RNS over LoRa\n"
            "Text + low-res image only  |  No voice  |  No telemetry",
            size=10, color=MUTED))

        scroll.add_widget(layout)
        self.add_widget(scroll)

    def save(self, *a):
        s = self.app_ref.settings
        s["rnode_mac"]      = self.rnode_mac.text.strip().upper()
        s["freq"]           = self.freq.text.strip()
        s["bw"]             = self.bw.text
        s["sf"]             = self.sf.text
        s["cr"]             = self.cr.text
        s["tx_power"]       = int(self.txp.text.strip() or 14)
        s["img_max_px"]     = int(self.img_px.text.strip() or 160)
        s["img_quality"]    = int(self.img_q.text.strip() or 55)
        s["display_name"]   = self.disp.text.strip()
        s["prop_node_hash"] = self.prop.text.strip()
        print("[SETTINGS] Saved: " + str(s))


class NanobandApp(App):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.core = NanobandCore()
        self.ble  = None
        self.settings = {
            "rnode_mac":      "F0:24:F9:B4:FB:6E",
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

        # status bar
        self.status_lbl = Label(
            text="NANOBAND  |  tap Connect",
            size_hint_y=None, height=dp(22),
            font_size=dp(10), color=MUTED, halign="center")
        root.add_widget(self.status_lbl)

        # connect bar
        conn_bar = BoxLayout(size_hint_y=None, height=dp(42),
                             padding=[dp(8), dp(4)])
        self.conn_btn = sbtn("CONNECT RNODE", h=34)
        self.conn_btn.bind(on_press=self.do_connect)
        conn_bar.add_widget(self.conn_btn)
        root.add_widget(conn_bar)

        root.add_widget(self.sm)

        # bottom nav
        nav = BoxLayout(size_hint_y=None, height=dp(52))
        with nav.canvas.before:
            Color(*SURFACE); Rectangle(pos=nav.pos, size=nav.size)
        for name, icon in [("messages","Msgs"),
                            ("contacts","Contacts"),
                            ("settings","Settings")]:
            b = Button(text=icon, background_normal="",
                       background_color=SURFACE, color=MUTED,
                       font_size=dp(12))
            b.bind(on_press=lambda x, n=name:
                setattr(self.sm, "current", n))
            nav.add_widget(b)
        root.add_widget(nav)

        # start RNS in background (no radio yet - just identity + router)
        config_dir = os.path.join(self.user_data_dir, "nanoband")
        os.makedirs(config_dir, exist_ok=True)
        self.config_dir = config_dir
        threading.Thread(
            target=self._init_rns, daemon=True).start()

        return root

    def _init_rns(self):
        ok = self.core.start(self.config_dir, self.settings["display_name"])
        def upd(dt):
            if ok:
                self.status_lbl.text = (
                    "NANOBAND  |  " + self.core.my_hash[:16] +
                    "  |  tap Connect")
                self.status_lbl.color = MUTED
            else:
                self.status_lbl.text = "NANOBAND  |  RNS FAILED"
                self.status_lbl.color = DANGER
        Clock.schedule_once(upd, 0)

    def do_connect(self, *a):
        s = self.settings
        self._set_conn("Connecting...", WARN, "CONNECTING...")
        self.ble = RNodeBLE(
            mac=s["rnode_mac"],
            on_status=self._ble_status,
            on_rx=self._ble_rx
        )
        # connect() is safe to call from main thread - able handles threading
        self.ble.connect()

    def _ble_status(self, text, color):
        def upd(dt):
            self.status_lbl.text  = "NANOBAND  |  " + text
            self.status_lbl.color = color
            if color == ACCENT:
                # BLE fully ready - send radio config
                s = self.settings
                self.ble.send_kiss_config(
                    s["freq"], s["bw"], s["sf"], s["cr"], s["tx_power"])
                self._set_conn("BLE LINKED", ACCENT, "RECONNECT")
            elif color == DANGER:
                self._set_conn(text[:20], DANGER, "RETRY")
        Clock.schedule_once(upd, 0)

    def _ble_rx(self, kiss_frame):
        # forward raw KISS frames from RNode into RNS
        # RNS reads from the RNodeInterface - for now just log
        print("[BLE RX] " + str(len(kiss_frame)) + " bytes")

    def _set_conn(self, status, color, btn_text):
        self.status_lbl.text  = "NANOBAND  |  " + status
        self.status_lbl.color = color
        self.conn_btn.text    = btn_text
        self.conn_btn.background_color = (
            ACCENT if color == ACCENT else
            DANGER if color == DANGER else WARN)

    def open_chat(self, dest_hash, dest_name):
        chat = self.sm.get_screen("chat")
        chat.load(dest_hash, dest_name)
        self.sm.current = "chat"


if __name__ == "__main__":
    NanobandApp().run()
