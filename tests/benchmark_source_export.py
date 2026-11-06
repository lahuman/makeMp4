"""Compare full MP4 export of a late original-video trim, with bounded hashing."""
import copy
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import editor_core as core
from seonyuldam import bundled_tool
import video_media


def main():
    root = Path(__file__).resolve().parents[1]/"build/source-export"
    root.mkdir(parents=True,exist_ok=True)
    work = root/("benchmark-"+uuid.uuid4().hex[:8]); work.mkdir()
    ffmpeg,ffprobe = bundled_tool("ffmpeg"),bundled_tool("ffprobe")
    source = root/"source-90s-1080p.mp4"
    if not source.exists():
        subprocess.run([ffmpeg,"-hide_banner","-nostdin","-v","error","-y","-f","lavfi","-i",
                        "testsrc2=s=1920x1080:r=30:d=90","-c:v","libx264","-preset","ultrafast",
                        "-crf","23","-g","90","-bf","2","-threads","2","-pix_fmt","yuv420p","-an",str(source)],
                       check=True,timeout=180,creationflags=video_media.FLAGS)
    asset = video_media.probe_video(source,ffprobe,threading.Event(),decode_origin=True)
    assert asset["decode_origin"] is not None and asset["frames"]==2700
    asset.update(id="v",path=str(source))
    project = core.fresh(); project["video_assets"] = [asset]
    clip = core.video_defaults("v",0,60)
    clip.update(source_in_frame=2400,source_out_frame=2460)
    project["videos"] = [clip]
    unchanged = copy.deepcopy(project)
    runs = []
    for repeat in range(3):
        for legacy in ((True,False) if repeat%2==0 else (False,True)):
            snapshot = copy.deepcopy(project)
            if legacy: snapshot["video_assets"][0]["decode_origin"] = None
            mode = "legacy" if legacy else "seek"
            output = work/f"{repeat+1}-{mode}.mp4"
            original = video_media.VideoDecoder._open
            starts = []
            def opened(decoder,number):
                original(decoder,number)
                starts.append(dict(frame=number,seek=decoder.used_seek))
            begin = time.perf_counter()
            with patch.object(video_media.VideoDecoder,"_open",autospec=True,side_effect=opened):
                core.export_video(snapshot,2,output,ffmpeg,threading.Event())
            elapsed = time.perf_counter()-begin
            run = dict(mode=mode,repeat=repeat+1,seconds=elapsed,output=str(output),decoder_starts=starts)
            runs.append(run); print(json.dumps(run),flush=True)
    assert project==unchanged
    expected = None
    for run in runs:
        streams = json.loads(subprocess.check_output([ffprobe,"-v","error","-show_streams","-of","json",run["output"]]))["streams"]
        video = next(s for s in streams if s["codec_type"]=="video")
        assert (video["codec_name"],int(video["nb_frames"]),video["width"],video["height"],video["color_space"])==("h264",60,1920,1080,"bt709")
        assert any(s["codec_name"]=="aac" for s in streams)
        digest = hashlib.sha256(); size = 0
        with tempfile.TemporaryFile(dir=work) as errors:
            process = subprocess.Popen([ffmpeg,"-v","error","-xerror","-i",run["output"],"-an",
                                        "-f","rawvideo","-pix_fmt","rgb24","pipe:1"],stdout=subprocess.PIPE,
                                       stderr=errors,creationflags=video_media.FLAGS)
            try:
                while raw:=process.stdout.read(1024*1024): digest.update(raw); size+=len(raw)
                assert process.wait(timeout=30)==0
            finally:
                if process.poll() is None: process.kill();process.wait()
                process.stdout.close()
        assert size==60*1920*1080*3
        if expected is None: expected=digest.hexdigest()
        assert digest.hexdigest()==expected,"Full decoded output differs"
        run["decoded_rgb_sha256"] = digest.hexdigest()
    medians = {mode:statistics.median(r["seconds"] for r in runs if r["mode"]==mode) for mode in ("legacy","seek")}
    result = dict(source=str(source),source_size=[1920,1080],source_seconds=90,source_trim=[80,82],output_frames=60,
                  quality=core.DEFAULT_EXPORT_QUALITY,encoder="libx264",runs=runs,medians=medians,
                  reduction_percent=(1-medians["seek"]/medians["legacy"])*100,matching_rgb_sha256=expected,
                  scope="Full export including audio mix, source decode, composition, encoding and mux; excludes source probe and validation decode")
    (root/"benchmark.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result),flush=True)


if __name__=="__main__": main()
