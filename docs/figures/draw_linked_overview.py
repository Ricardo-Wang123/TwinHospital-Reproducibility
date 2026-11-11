"""Draw the encounter overview as a publication-width vector PDF."""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Arc, Circle, FancyArrowPatch, FancyBboxPatch, Rectangle


WIDTH, HEIGHT = 454, 239
INK = "#20354b"
NAVY = "#234463"
MUTED = "#466078"
LINE = "#a0b1c1"
TEAL = "#246861"
PLUM = "#665581"
REVISE = "#9b4b2a"

matplotlib.rcParams.update({"font.family": "Times New Roman", "pdf.fonttype": 42})
font_manager.findfont("Times New Roman", fallback_to_default=False)
fig = plt.figure(figsize=(WIDTH / 72, HEIGHT / 72), facecolor="white")
ax = fig.add_axes((0, 0, 1, 1), xlim=(0, WIDTH), ylim=(HEIGHT, 0))
ax.set_axis_off()


def label(x, y, value, *, size=10.5, bold=False, color=INK, align="left"):
    ax.text(x, y, value, fontsize=size, fontweight="bold" if bold else "normal",
            color=color, ha=align, va="center")


def line(points, color, *, width=0.85, style="solid"):
    xs, ys = zip(*points)
    ax.plot(xs, ys, color=color, linewidth=width, linestyle=style,
            solid_capstyle="round", solid_joinstyle="round")


def arrow(start, end, color=NAVY):
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=8.5,
                                 color=color, linewidth=1.05, shrinkA=0, shrinkB=0))


def panel(x, y, width, height, *, fill="white", edge=LINE):
    ax.add_patch(FancyBboxPatch(
        (x, y), width, height, boxstyle="round,pad=0,rounding_size=3",
        facecolor=fill, edgecolor=edge, linewidth=0.65))


def glyph(name, x, y, color):
    def stroke(*offsets):
        line([(x + dx, y + dy) for dx, dy in offsets], color, width=1)

    if name == "id":
        ax.add_patch(FancyBboxPatch(
            (x - 7, y - 5), 14, 10, boxstyle="round,pad=0,rounding_size=1.4",
            fill=False, edgecolor=color, linewidth=1))
        ax.add_patch(Circle((x - 3.5, y - 1.5), 1.2, fill=False,
                            edgecolor=color, linewidth=0.9))
        stroke((-5, 2.5), (-3.5, 1.3), (-2, 2.5))
        stroke((0, -1.6), (4.5, -1.6))
        stroke((0, 1.5), (4.5, 1.5))
    elif name == "chat":
        stroke((-6, 3), (-6, -5), (6, -5), (6, 3), (-0.7, 3),
               (-4.5, 6.5), (-4.5, 3), (-6, 3))
        stroke((-3.5, -2), (3.5, -2))
        stroke((-3.5, 0.3), (1.7, 0.3))
    elif name == "care":
        stroke((-6.5, 0), (-3.8, 0), (-1.7, -5.5), (0.8, 5.5),
               (3, -0.3), (6.5, -0.3))
    elif name == "shield":
        stroke((0, -7), (6, -4), (5.4, 2.4), (0, 7), (-5.4, 2.4),
               (-6, -4), (0, -7))
        stroke((-2.6, 0), (-0.6, 2), (3, -2.3))
    elif name == "lock":
        ax.add_patch(Arc((x, y - 2), 7, 9, theta1=180, theta2=360,
                         edgecolor=color, linewidth=1))
        stroke((-3.5, -2), (-3.5, 0), (3.5, 0), (3.5, -2))
        ax.add_patch(Rectangle((x - 5.5, y), 11, 6.5, fill=False,
                               edgecolor=color, linewidth=1))
        stroke((0, 2.3), (0, 4.2))
    elif name == "receipt":
        stroke((-5.5, 6), (-5.5, -6), (5.5, -6), (5.5, 6),
               (2.8, 4.5), (0, 6), (-2.8, 4.5), (-5.5, 6))
        stroke((-2.8, -2.7), (2.8, -2.7))
        stroke((-2.8, 0.4), (1, 0.4))
    else:
        raise ValueError(f"Unknown glyph: {name}")


for x, icon, heading, detail in (
    (3, "id", "Case inputs", "MIMIC-IV history and constructed visit"),
    (232, "chat", "Role-specific views", "Patient memory and authorized records"),
):
    panel(x, 2, 219, 33, fill="#f4f7fa")
    glyph(icon, x + 15, 18.5, NAVY)
    label(x + 29, 12, heading, bold=True, size=11)
    label(x + 29, 25, detail, color=MUTED)
    arrow((x + 109.5, 36.5), (x + 109.5, 43), color=MUTED)

ax.add_patch(Rectangle((3, 44), 448, 18, facecolor=NAVY, edgecolor="none"))
label(13, 53, "One linked outpatient encounter", color="white", bold=True, size=12)
label(441, 53, "Role, patient, version and transaction checks",
      color="white", align="right")

for start, end, fill in (
    (3, 152, "#f2f8f7"), (152, 302, "#f3f6fa"), (302, 451, "#f7f4fa"),
):
    ax.add_patch(Rectangle((start, 62), end - start, 78, facecolor=fill, edgecolor="none"))
    line([(start, 62), (start, 140)], LINE, width=0.6)
line([(3, 140), (451, 140), (451, 62)], LINE, width=0.6)

stages = (
    (40, "id", "Register", "Encounter ID", TEAL, "#e0efec"),
    (115, "chat", "Pre-consult", "Saved handoff", TEAL, "#e0efec"),
    (190, "care", "Consult", "Proposed order", NAVY, "#e4edf6"),
    (264, "shield", "Review & sign", "Signed order", NAVY, "#e4edf6"),
    (339, "lock", "Attest note", "Locked version", PLUM, "#ebe4f2"),
    (414, "receipt", "Settle", "Payment receipt", PLUM, "#ebe4f2"),
)
for x, icon, heading, detail, color, fill in stages:
    ax.add_patch(Circle((x, 99), 11.5, facecolor=fill, edgecolor="none"))
    glyph(icon, x, 99, color)
    label(x, 119, heading, size=11, bold=True, align="center")
    label(x, 132, detail, color=MUTED, align="center")
for left, right in zip(stages, stages[1:]):
    arrow((left[0] + 15, 99), (right[0] - 15, 99))

line([(264, 86), (264, 78), (190, 78)], REVISE, style=(0, (2.3, 1.8)))
arrow((190, 78), (190, 86), color=REVISE)
label(227, 70.5, "Review or encoding feedback", color=REVISE, align="center")

arrow((227, 141), (227, 149))
panel(3, 150, 448, 32, fill="#eef3f7", edge="#90a6bb")
label(14, 158, "TwinHospital adapter", bold=True, size=11)
label(14, 174, "Queue, chart and handoff", color=MUTED)
label(315, 158, "OpenHIS records", bold=True, size=11)
label(315, 174, "Orders, notes and payments", color=MUTED)

line([(137, 158), (169, 158)], NAVY)
label(227, 158, "FHIR R4 / WS/T services", align="center", size=10.5)
arrow((284, 158), (306, 158))
line([(306, 174), (284, 174)], TEAL)
label(227, 174, "Verified state read-back", align="center", size=10.5, color=TEAL)
arrow((169, 174), (137, 174), color=TEAL)

label(227, 190, "Versioned encounter trace", bold=True, size=11, align="center")
line([(227, 196), (227, 200), (75, 200)], MUTED)
line([(227, 200), (379, 200)], MUTED)
for x, numeral, heading, detail in (
    (3, "I", "Clinical artifacts", "Handoff, prescription, note"),
    (155, "II", "Modeled staff effort", "Activity through delivery"),
    (307, "III", "Workflow integrity", "State checks and fault probes"),
):
    arrow((x + 72, 200), (x + 72, 206), color=MUTED)
    panel(x, 207, 144, 29, edge="#b4c2cf")
    ax.add_patch(Circle((x + 12, 216), 7, facecolor=NAVY, edgecolor="none"))
    label(x + 12, 216, numeral, size=9.5, bold=True, color="white", align="center")
    label(x + 23, 216, heading, size=11, bold=True)
    label(x + 72, 229, detail, color=MUTED, align="center")

fig.savefig(Path(sys.argv[1]), format="pdf", metadata={"CreationDate": None, "ModDate": None})
