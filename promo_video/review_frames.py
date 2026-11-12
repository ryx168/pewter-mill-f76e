"""Extract one frame per key moment into renders/review/ and a contact sheet, for visual QA."""
import json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
mp4 = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "renders", "draft1.mp4")
out = os.path.join(HERE, "renders", "review"); os.makedirs(out, exist_ok=True)
for f in os.listdir(out): os.remove(os.path.join(out, f))
T = json.load(open(os.path.join(HERE, "timing.json"), encoding="utf-8"))
times = []
for s in T["scenes"]:
    times.append((s["id"] + "_a", s["start"] + 1.2))
    times.append((s["id"] + "_b", s["units"][-1]["end"] - 0.3))
    if len(s["units"]) > 2: times.append((s["id"] + "_m", s["units"][1]["end"] + 0.8))
times.append(("end", T["total"] - 2.2))
for name, t in sorted(times, key=lambda x: x[1]):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", mp4, "-frames:v", "1", "-vf", "scale=960:540", os.path.join(out, f"{t:06.2f}_{name}.jpg")], check=True)
files = sorted(os.listdir(out))
subprocess.run(["ffmpeg", "-v", "error", "-y", "-pattern_type", "glob", "-i", os.path.join(out, "*.jpg"), "-filter_complex", f"tile=3x{(len(files)+2)//3}", os.path.join(HERE, "renders", "review_sheet.jpg")], check=False)
print(len(files), "frames ->", out)
