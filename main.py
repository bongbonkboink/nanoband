import os, time, threading
from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.image import Image as KivyImage
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.clock import Clock
from kivy.utils import get_color_from_hex as hex
from kivy.graphics import Color, Rectangle, RoundedRectangle
from PIL import Image as PILImage
import io, base64

Window.clearcolor = hex("0a0c0f")

BG      = hex("0a0c0f")
SURFACE = hex("111418")
CARD    = hex("161b22")
BORDER  = hex("1e2530")
ACCENT  = hex("00e5a0")
MUTED   = hex("556070")
TEXT    = hex("d4dce8")
DANGER  = hex("e05050")

# ── RNS / LXMF bootstrap ────────────────────────────────────────────────────
try:
    import RNS
    import LXMF
    RNS_AVAILABLE = True
except ImportError:
    RNS_AVAILABLE = False

class NanobandCore:
    def __init__(self):
        self.reticulum   = None
        self.router      = None
        self.identity    = None
        self.destination = None
        self.ready       = False
        self.messages    = {}   # {hash_str: [msg_dict, ...]}
        self.contacts    = {}   # {hash_str: display_name}

    def start(self, config_path, display_name="Nanoband User"):
        if not RNS_AVAILABLE:
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
            return True
        except Exception as e:
            print(f"[CORE] Start error: {e}")
            return False

    def _on_receive(self, message):
        sender = RNS.prettyhexrep(message.source_hash)
        content = message.content.decode("utf-8", errors="replace")
        ts = time.time()
        if sender not in self.messages:
            self.messages[sender] = []
        self.messages[sender].append({
            "from": sender, "txt": content,
            "img": None, "ts": ts
        })

    def send(self, dest_hash_str, text, img_b64=None):
        if not self.ready:
            return False
        try:
            dest_hash = bytes.fromhex(dest_hash_str.replace("<","").replace(">","").replace(":",""))
            if not RNS.Transport.has_path(dest_hash):
                RNS.Transport.request_path(dest_hash)
                timeout = 10
                while not RNS.Transport.has_path(dest_hash) and timeout > 0:
                    time.sleep(0.5); timeout -= 0.5
            id_dest = RNS.Identity.recall(dest_hash)
            if id_dest is None:
                return False
            lxmf_dest = RNS.Destination(
                id_dest, RNS.Destination.OUT,
                RNS.Destination.SINGLE, "lxmf", "delivery"
            )
            content = text.encode("utf-8")
            msg = LXMF.LXMessage(
                lxmf_dest, self.destination,
                content, desired_method=LXMF.LXMessage.DIRECT
            )
            self.router.handle_outbound(msg)
            return True
        except Exception as e:
            print(f"[CORE] Send error: {e}")
            return False

    @property
    def my_hash(self):
        if self.identity:
            return RNS.prettyhexrep(self.destination.hash)
        return "<not started>"


# ── UI Helpers ───────────────────────────────────────────────────────────────
def styled_btn(text, bg=ACCENT, color=BG, height=44):
    b = Button(
        text=text, size_hint_y=None, height=dp(height),
        background_normal="", background_color=bg,
        color=color, font_size=dp(13), bold=True
    )
    return b

def styled_input(hint="", multiline=False, height=40):
    t = TextInput(
        hint_text=hint, multiline=multiline,
        size_hint_y=None, height=dp(height),
        background_color=CARD, foreground_color=TEXT,
        hint_text_color=MUTED, cursor_color=ACCENT,
        font_size=dp(13), padding=[dp(10), dp(10)],
    )
    return t

def lbl(text, size=13, color=TEXT, bold=False):
    return Label(
        text=text, font_size=dp(size), color=color,
        bold=bold, size_hint_y=None,
        height=dp(size * 1.8), halign="left",
        text_size=(None, None)
    )

def compress_image(path, max_px=160, quality=55):
    try:
        img = PILImage.open(path).convert("RGB")
        ratio = min(max_px/img.width, max_px/img.height)
        if ratio < 1:
            img = img.resize(
                (int(img.width*ratio), int(img.height*ratio)),
                PILImage.LANCZOS
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[IMG] Compress error: {e}")
        return None


# ── Screens ──────────────────────────────────────────────────────────────────
class MessagesScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="messages", **kw)
        self.app_ref = app_ref
        layout = BoxLayout(orientation="vertical", spacing=0)

        # header
        hdr = BoxLayout(size_hint_y=None, height=dp(44),
                        padding=[dp(14),0])
        with hdr.canvas.before:
            Color(*SURFACE); Rectangle(pos=hdr.pos, size=hdr.size)
        hdr.add_widget(lbl("MESSAGES", size=12, color=MUTED, bold=True))
        layout.add_widget(hdr)

        # list
        self.scroll = ScrollView()
        self.list   = BoxLayout(orientation="vertical", size_hint_y=None,
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
                "No conversations yet.\nGo to Contacts to start.",
                color=MUTED, size=12
            ))
            return
        for h, msgs in messages.items():
            if not msgs: continue
            last = msgs[-1]
            name = contacts.get(h, h[:16])
            btn = Button(
                text=f"[b]{name}[/b]\n{last.get('txt','[image]')[:40]}",
                markup=True, size_hint_y=None, height=dp(60),
                background_normal="", background_color=SURFACE,
                color=TEXT, font_size=dp(13), halign="left",
                text_size=(Window.width - dp(28), None)
            )
            btn.bind(on_press=lambda x, h=h, n=name:
                self.app_ref.open_chat(h, n))
            self.list.add_widget(btn)


class ContactsScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="contacts", **kw)
        self.app_ref = app_ref
        layout = BoxLayout(orientation="vertical", spacing=0,
                           padding=[dp(14), dp(8)])
        layout.add_widget(lbl("CONTACTS", size=12, color=MUTED, bold=True))

        self.search = styled_input("Search name or hash…")
        layout.add_widget(self.search)

        self.scroll = ScrollView()
        self.list   = BoxLayout(orientation="vertical", size_hint_y=None,
                                spacing=dp(4))
        self.list.bind(minimum_height=self.list.setter("height"))
        self.scroll.add_widget(self.list)
        layout.add_widget(self.scroll)

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.name_in = styled_input("Display name")
        self.hash_in = styled_input("Hash e.g. a1b2c3…")
        add_btn = styled_btn("ADD")
        add_btn.bind(on_press=self.add_contact)
        row.add_widget(self.name_in)
        row.add_widget(self.hash_in)
        row.add_widget(add_btn)
        layout.add_widget(row)

        self.add_widget(layout)
        self.refresh()

    def add_contact(self, *a):
        name = self.name_in.text.strip()
        h    = self.hash_in.text.strip().lower().replace(" ","")
        if name and h:
            self.app_ref.core.contacts[h] = name
            self.name_in.text = ""
            self.hash_in.text = ""
            self.refresh()

    def refresh(self):
        self.list.clear_widgets()
        for h, name in self.app_ref.core.contacts.items():
            btn = Button(
                text=f"[b]{name}[/b]  [color=556070]{h[:20]}…[/color]",
                markup=True, size_hint_y=None, height=dp(52),
                background_normal="", background_color=CARD,
                color=TEXT, font_size=dp(13)
            )
            btn.bind(on_press=lambda x, h=h, n=name:
                (self.app_ref.open_chat(h, n),
                 setattr(self.app_ref.sm, "current", "messages")))
            self.list.add_widget(btn)


class ChatScreen(Screen):
    def __init__(self, app_ref, **kw):
        super().__init__(name="chat", **kw)
        self.app_ref     = app_ref
        self.dest_hash   = None
        self.dest_name   = ""
        layout = BoxLayout(orientation="vertical")

        # header
        hdr = BoxLayout(size_hint_y=None, height=dp(48),
                        padding=[dp(8), 0], spacing=dp(8))
        with hdr.canvas.before:
            Color(*SURFACE); Rectangle(pos=hdr.pos, size=hdr.size)
        back = styled_btn("‹", bg=SURFACE, color=ACCENT, height=44)
        back.size_hint_x = None
        back.width = dp(40)
        back.bind(on_press=lambda x:
            setattr(self.app_ref.sm, "current", "messages"))
        self.title_lbl = lbl("", size=14, bold=True)
        hdr.add_widget(back)
        hdr.add_widget(self.title_lbl)
        layout.add_widget(hdr)

        # messages
        self.scroll = ScrollView()
        self.msg_list = BoxLayout(orientation="vertical",
                                  size_hint_y=None, spacing=dp(6),
                                  padding=[dp(12), dp(8)])
        self.msg_list.bind(minimum_height=self.msg_list.setter("height"))
        self.scroll.add_widget(self.msg_list)
        layout.add_widget(self.scroll)

        # input row
        inp_row = BoxLayout(size_hint_y=None, height=dp(52),
                            padding=[dp(8), dp(6)], spacing=dp(6))
        with inp_row.canvas.before:
            Color(*SURFACE); Rectangle(pos=inp_row.pos, size=inp_row.size)
        self.text_in = styled_input("Type message…", height=40)
        self.text_in.size_hint_x = 0.75
        img_btn  = styled_btn("📷", bg=CARD, color=TEXT, height=40)
        img_btn.size_hint_x  = None
        img_btn.width = dp(42)
        send_btn = styled_btn("▲", height=40)
        send_btn.size_hint_x = None
        send_btn.width = dp(42)
        img_btn.bind(on_press=self.pick_image)
        send_btn.bind(on_press=self.send_msg)
        inp_row.add_widget(self.text_in)
        inp_row.add_widget(img_btn)
        inp_row.add_widget(send_btn)
        layout.add_widget(inp_row)

        self.add_widget(layout)

    def load(self, dest_hash, dest_name):
        self.dest_hash = dest_hash
        self.dest_name = dest_name
        self.title_lbl.text = dest_name
        self.refresh()

    def refresh(self):
        self.msg_list.clear_widgets()
        msgs = self.app_ref.core.messages.get(self.dest_hash, [])
        for m in msgs:
            is_me = m["from"] == "me"
            align_box = BoxLayout(size_hint_y=None, height=dp(48))
            if is_me:
                align_box.add_widget(Label(size_hint_x=0.25))
            bubble = Button(
                text=m.get("txt","[image]"),
                size_hint_x=0.75, size_hint_y=None, height=dp(48),
                background_normal="",
                background_color=hex("00b87a33") if is_me else CARD,
                color=TEXT, font_size=dp(13),
                halign="right" if is_me else "left",
                text_size=(Window.width * 0.65, None)
            )
            align_box.add_widget(bubble)
            if not is_me:
                align_box.add_widget(Label(size_hint_x=0.25))
            self.msg_list.add_widget(align_box)
        Clock.schedule_once(lambda dt:
            setattr(self.scroll, "scroll_y", 0), 0.1)

    def send_msg(self, *a):
        txt = self.text_in.text.strip()
        if not txt: return
        core = self.app_ref.core
        if self.dest_hash not in core.messages:
            core.messages[self.dest_hash] = []
        core.messages[self.dest_hash].append({
            "from":"me", "txt":txt, "img":None, "ts":time.time()
        })
        threading.Thread(
            target=core.send, args=(self.dest_hash, txt), daemon=True
        ).start()
        self.text_in.text = ""
        self.refresh()

    def pick_image(self, *a):
        # On Android, use camera intent via plyer
        try:
            from plyer import camera
            camera.take_picture(
                filename=os.path.join(
                    App.get_running_app().user_data_dir, "capture.jpg"),
                on_complete=self.on_image_captured
            )
        except Exception as e:
            print(f"[CAM] {e}")

    def on_image_captured(self, path):
        if not path or not os.path.exists(path): return
        settings = self.app_ref.settings
        b64 = compress_image(
            path,
            max_px=settings.get("img_max_px", 160),
            quality=settings.get("img_quality", 55)
        )
        if not b64: return
        core = self.app_ref.core
        if self.dest_hash not in core.messages:
            core.messages[self.dest_hash] = []
        core.messages[self.dest_hash].append({
            "from":"me", "txt":"[image]", "img":b64, "ts":time.time()
        })
        threading.Thread(
            target=core.send,
            args=(self.dest_hash, f"[IMG]{b64}"), daemon=True
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

        def section(title):
            layout.add_widget(lbl(title, size=10, color=ACCENT, bold=True))

        def row(label, widget):
            r = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            r.add_widget(lbl(label, size=13))
            r.add_widget(widget)
            layout.add_widget(r)

        s = app_ref.settings

        section("RNODE · BLUETOOTH")
        self.rnode_name = styled_input("RNode device name", height=40)
        self.rnode_name.text = s.get("rnode_name","")
        row("Paired RNode", self.rnode_name)

        section("RADIO PARAMETERS")
        self.freq = styled_input("e.g. 868.125", height=40)
        self.freq.text = s.get("freq","868.125")
        row("Frequency (MHz)", self.freq)

        from kivy.uix.spinner import Spinner
        self.bw = Spinner(
            text=s.get("bw","125"),
            values=["125","250","500"],
            size_hint_y=None, height=dp(40),
            background_color=CARD, color=TEXT
        )
        row("Bandwidth (kHz)", self.bw)

        self.sf = Spinner(
            text=s.get("sf","9"),
            values=[str(n) for n in range(7,13)],
            size_hint_y=None, height=dp(40),
            background_color=CARD, color=TEXT
        )
        row("Spreading Factor", self.sf)

        self.txpower = styled_input("1-22", height=40)
        self.txpower.text = str(s.get("tx_power",14))
        row("TX Power (dBm)", self.txpower)

        section("IMAGE TRANSFER")
        self.img_px = styled_input("pixels", height=40)
        self.img_px.text = str(s.get("img_max_px",160))
        row("Max image width", self.img_px)

        self.img_q = styled_input("20-80", height=40)
        self.img_q.text = str(s.get("img_quality",55))
        row("JPEG quality (%)", self.img_q)

        section("IDENTITY")
        self.disp_name = styled_input("Your display name", height=40)
        self.disp_name.text = s.get("display_name","Field Unit")
        row("Display name", self.disp_name)

        layout.add_widget(lbl(
            f"Your hash: {app_ref.core.my_hash}",
            size=11, color=ACCENT
        ))

        section("RETICULUM NETWORK")
        self.prop_hash = styled_input("Propagation node hash", height=40)
        self.prop_hash.text = s.get("prop_node_hash","")
        row("Prop node hash", self.prop_hash)

        save_btn = styled_btn("SAVE SETTINGS")
        save_btn.bind(on_press=self.save)
        layout.add_widget(save_btn)

        layout.add_widget(lbl(
            "NANOBAND v0.1\nLXMF · Text + low-res image\nNo voice · No telemetry",
            size=10, color=MUTED
        ))

        scroll.add_widget(layout)
        self.add_widget(scroll)

    def save(self, *a):
        s = self.app_ref.settings
        s["rnode_name"]    = self.rnode_name.text.strip()
        s["freq"]          = self.freq.text.strip()
        s["bw"]            = self.bw.text
        s["sf"]            = self.sf.text
        s["tx_power"]      = int(self.txpower.text.strip() or 14)
        s["img_max_px"]    = int(self.img_px.text.strip() or 160)
        s["img_quality"]   = int(self.img_q.text.strip() or 55)
        s["display_name"]  = self.disp_name.text.strip()
        s["prop_node_hash"]= self.prop_hash.text.strip()


# ── App ───────────────────────────────────────────────────────────────────────
class NanobandApp(App):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.core = NanobandCore()
        self.settings = {
            "rnode_name":    "RNode_A3F2",
            "freq":          "868.125",
            "bw":            "125",
            "sf":            "9",
            "tx_power":      14,
            "img_max_px":    160,
            "img_quality":   55,
            "display_name":  "Field Unit",
            "prop_node_hash":"",
        }

    def build(self):
        self.sm = ScreenManager()
        self.sm.add_widget(MessagesScreen(self))
        self.sm.add_widget(ContactsScreen(self))
        self.sm.add_widget(ChatScreen(self))
        self.sm.add_widget(SettingsScreen(self))

        root = BoxLayout(orientation="vertical")

        # status bar
        self.status_bar = Label(
            text="● NANOBAND  |  LXMF/RNS",
            size_hint_y=None, height=dp(24),
            font_size=dp(10), color=ACCENT,
            halign="center"
        )
        root.add_widget(self.status_bar)
        root.add_widget(self.sm)

        # bottom nav
        nav = BoxLayout(size_hint_y=None, height=dp(52))
        with nav.canvas.before:
            Color(*SURFACE); Rectangle(pos=nav.pos, size=nav.size)
        for name, icon in [("messages","◈ Msgs"),
                            ("contacts","◉ Contacts"),
                            ("settings","◎ Settings")]:
            b = Button(
                text=icon, background_normal="",
                background_color=SURFACE, color=MUTED,
                font_size=dp(12)
            )
            b.bind(on_press=lambda x, n=name:
                setattr(self.sm, "current", n))
            nav.add_widget(b)
        root.add_widget(nav)

        # start RNS in background
        config_dir = os.path.join(self.user_data_dir, "rns_config")
        os.makedirs(config_dir, exist_ok=True)
        threading.Thread(
            target=self.core.start,
            args=(config_dir, self.settings["display_name"]),
            daemon=True
        ).start()

        return root

    def open_chat(self, dest_hash, dest_name):
        chat = self.sm.get_screen("chat")
        chat.load(dest_hash, dest_name)
        self.sm.current = "chat"


if __name__ == "__main__":
    NanobandApp().run()