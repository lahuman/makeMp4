"""Compare the compositor with a saved pre-change editor_core.py, without codecs."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
import editor_core as core


class Frames:
    def __init__(self):
        self.image = Image.new("RGB", (640,360), (45,100,185))
        draw = ImageDraw.Draw(self.image)
        for x in range(0,640,13): draw.line((x,0,640-x,359), fill=(x%256,180,95), width=3)

    def begin_frame(self, clips): pass
    def get(self, clip, frame): return self.image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("compositor_baseline",args.baseline.resolve())
    baseline = importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline)
    root = Path(__file__).resolve().parents[1] / "build/media-quality"
    root.mkdir(parents=True,exist_ok=True)
    cases = []
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        folder = Path(temporary)
        background = folder/"background.png"
        Image.new("RGB",(1920,1080),(50,90,140)).save(background)
        overlay = folder/"overlay.png"
        image = Image.new("RGBA",(320,180),(0,0,0,0))
        ImageDraw.Draw(image).rounded_rectangle((4,4,315,175),radius=30,fill=(180,60,110,150))
        image.save(overlay)
        for mode in ("opaque_image", "video_overlays", "moving_images"):
            project = core.fresh()
            project["assets"] = [dict(id="bg",path=str(background)),dict(id="overlay",path=str(overlay))]
            project["images"] = [core.image_defaults("bg",0,60)]
            if mode == "video_overlays":
                project["images"].clear()
                project["video_assets"] = [dict(id="v",path="synthetic",width=640,height=360)]
                project["videos"] = [core.video_defaults("v",0,60)]
            if mode != "opaque_image":
                for index in range(3):
                    cue = core.image_defaults("overlay",0,60)
                    cue["from"].update(x=200+index*650,y=850,zoom=18)
                    cue["to"].update(x=300+index*650,y=700,zoom=25)
                    cue["motion"] = mode == "moving_images"
                    project["images"].append(cue)
                project["texts"] = [dict(id="t",start=0,end=60,text="Composite · 영상 품질",size=36,
                                          x=960,y=100,width=650,background_alpha=120)]
            samples = []
            expected = None
            for repeat in range(3):
                engines = (("baseline",baseline),("optimized",core))
                if repeat % 2: engines = engines[::-1]
                for name, engine in engines:
                    frames, cache = Frames(), engine.LayerCache()
                    engine.render_scene(project,0,video_frames=frames,cache=cache)
                    digest = hashlib.sha256()
                    begin = time.perf_counter()
                    for n in range(24):
                        picture = engine.render_scene(project,n,video_frames=frames,cache=cache)
                        digest.update(picture.tobytes())
                    elapsed = time.perf_counter()-begin
                    if expected is None: expected = digest.hexdigest()
                    assert digest.hexdigest() == expected, (mode,name,"pixel mismatch")
                    run = dict(mode=name,repeat=repeat+1,seconds=elapsed)
                    samples.append(run); print(json.dumps(dict(case=mode,**run)),flush=True)
            medians = {name:statistics.median(s["seconds"] for s in samples if s["mode"]==name)
                       for name in ("baseline","optimized")}
            cases.append(dict(case=mode,size=[1920,1080],frames=24,runs=samples,medians=medians,
                              matching_sha256=expected,reduction_percent=(1-medians["optimized"]/medians["baseline"])*100))
    result = dict(baseline=str(args.baseline.resolve()),baseline_sha256=hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
                  optimized_sha256=hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest(),cases=cases,
                  scope="Warm-cache Pillow compositor plus RGB bytes/hash; excludes decoding, encoding and UI")
    (root/"composite-benchmark.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result),flush=True)


if __name__ == "__main__": main()
