"""Rounded ttk surfaces with native focus, keyboard and disabled behavior."""
from PIL import Image, ImageDraw, ImageTk


def install_studio_controls(root, style, colors):
    images = []

    def surface(fill, stroke, radius=7, stroke_width=1):
        scale = 4
        picture = Image.new("RGBA", (32 * scale, 32 * scale))
        draw = ImageDraw.Draw(picture)
        draw.rounded_rectangle((2, 2, 126, 126), radius=radius * scale,
                               fill=fill, outline=stroke, width=scale * stroke_width)
        photo = ImageTk.PhotoImage(picture.resize((32, 32), Image.Resampling.LANCZOS), master=root)
        images.append(photo)
        return photo

    c = colors
    control_border = "#8B95AA"
    for name, fill, stroke, hover, ink in (
        ("TButton", "#F7F8FC", control_border, "#E8E7FF", c["text"]),
        ("Primary.TButton", c["primary"], c["primary"], c["primary_hover"], "#FFFFFF"),
        ("Tint.TButton", c["primary_tint"], c["primary"], "#DCD9FF", "#403EA9"),
        ("Quiet.TButton", c["surface"], control_border, "#E8E7FF", c["text"]),
        ("Ghost.TButton", "#F7F8FC", control_border, "#E8E7FF", c["text"]),
        ("Section.TButton", "#F2F3F8", control_border, "#E8E7FF", c["text"]),
        ("Danger.TButton", "#FFF4F4", "#BC7272", "#FFE1E1", "#A12E2E"),
    ):
        normal = surface(fill, stroke)
        active = surface(hover, c["primary"] if name != "Danger.TButton" else stroke)
        pressed = surface(hover, c["text"], stroke_width=2)
        focus = surface(fill, c["primary"], stroke_width=2)
        disabled = surface(c["surface_alt"], c["border"])
        element = "Studio." + name + ".surface"
        style.element_create(element, "image", normal, ("disabled", disabled),
                             ("pressed", pressed), ("active", active), ("focus", focus),
                             border=9, padding=0, sticky="nsew")
        style.layout(name, [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})])
        style.configure(name, padding=(11, 7), foreground=ink, background=c["surface"],
                        anchor="center", borderwidth=0)
        style.map(name, foreground=[("disabled", c["muted"]), ("!disabled", ink)])
    style.configure("Section.TButton", anchor="w", padding=(9, 8))
    style.configure("Quiet.TButton", background=c["surface_alt"], padding=(5, 6))
    style.configure("Ghost.TButton", padding=(6, 6))
    style.configure("Time.TButton", font=("Consolas", 9), padding=(6, 6))

    normal = surface(c["surface_alt"], c["border"], 5)
    focus = surface(c["surface"], c["primary"], 5)
    style.element_create("Studio.Entry.field", "image", normal, ("focus", focus),
                         border=7, padding=0, sticky="nsew")
    style.layout("TEntry", [("Studio.Entry.field", {"sticky": "nsew", "children": [
        ("Entry.padding", {"sticky": "nsew", "children": [
            ("Entry.textarea", {"sticky": "nsew"})]})]})])
    style.configure("TEntry", padding=(9, 7), borderwidth=0)
    combo = surface(c["surface"], control_border, 5)
    combo_focus = surface(c["surface"], c["primary"], 5, stroke_width=2)
    style.element_create("Studio.Combo.field", "image", combo, ("focus", combo_focus),
                         border=7, padding=0, sticky="nsew")
    style.layout("TCombobox", [("Studio.Combo.field", {"sticky": "nsew", "children": [
        ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"sticky": "nsew", "children": [
            ("Combobox.textarea", {"sticky": "nsew"})]})]})])
    style.configure("TCombobox", padding=(9, 7), arrowsize=12, borderwidth=0)
    style.map("TCombobox", fieldbackground=[("readonly", c["surface_alt"])],
              selectbackground=[("readonly", c["surface_alt"])])

    for name in ("TMenubutton", "Chrome.TMenubutton"):
        normal = surface("#F7F8FC", control_border)
        active = surface(c["primary_tint"], c["primary"])
        focus = surface("#F7F8FC", c["primary"], stroke_width=2)
        element = "Studio." + name + ".surface"
        style.element_create(element, "image", normal, ("active", active), ("focus", focus),
                             border=9, padding=0, sticky="nsew")
        children = [("Menubutton.indicator", {"side": "right", "sticky": ""}),
                    ("Menubutton.label", {"sticky": "we", "expand": True})]
        style.layout(name, [(element, {"sticky": "nsew", "children": [
            ("Menubutton.padding", {"sticky": "nsew", "children": children})]})])
        style.configure(name, padding=(9, 7), foreground=c["text"], borderwidth=0)

    normal = surface(c["surface"], control_border)
    selected = surface(c["primary_tint"], c["primary"])
    focus = surface(c["primary_tint"], c["primary"], stroke_width=2)
    style.element_create("Studio.Chip.surface", "image", normal,
                         ("focus", focus), ("selected", selected), ("active", selected),
                         border=9, padding=0, sticky="nsew")
    style.layout("Chip.TRadiobutton", [("Studio.Chip.surface", {"sticky": "nsew", "children": [
        ("Radiobutton.padding", {"sticky": "nsew", "children": [
            ("Radiobutton.label", {"sticky": "nsew"})]})]})])
    style.configure("Chip.TRadiobutton", padding=(9, 6), foreground=c["text"])
    style.map("Chip.TRadiobutton", foreground=[("selected", c["primary"])])
    style.configure("TScrollbar", arrowsize=16, borderwidth=1, gripsize=8,
                    background="#8792A8", troughcolor="#E6E9F1", arrowcolor=c["text"],
                    bordercolor="#8792A8", lightcolor="#8792A8", darkcolor="#8792A8")
    style.map("TScrollbar", background=[("pressed", c["primary_hover"]), ("active", c["primary"])])
    style.configure("ScrollHint.TLabel", foreground="#403EA9", background=c["surface"],
                    font=("Malgun Gothic", 9, "bold"))
    style.configure("Drag.TLabel", foreground="#403EA9", background=c["primary_tint"],
                    relief="solid", borderwidth=1, padding=(6, 6))
    return images


def rounded_clip(canvas, x0, y0, x1, y1, **options):
    radius = min(6, max(0, (x1 - x0) / 2), (y1 - y0) / 2)
    return canvas.create_polygon(
        x0 + radius, y0, x1 - radius, y0, x1, y0, x1, y0 + radius,
        x1, y1 - radius, x1, y1, x1 - radius, y1, x0 + radius, y1,
        x0, y1, x0, y1 - radius, x0, y0 + radius, x0, y0,
        smooth=True, splinesteps=16, **options)
