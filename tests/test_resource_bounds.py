"""Resource bounds must preserve pixels, samples and cancellation behavior."""
from array import array
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import subprocess
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops

import editor_core as core
import video_media
from seonyuldam import bundled_tool
from editor_ui_v2 import EditorApp


class ResourceBoundsTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1]/"build"/"resource-tests"
        base.mkdir(parents=True,exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.folder.cleanup)
        self.work = Path(self.folder.name)

    def test_audio_mixer_matches_reference_with_many_sources_and_seeks(self):
        clips, paths, reference = [], {}, {}
        for index in range(24):
            ident = str(index)
            raw = array("f",[.001*(index+1),-.0003*(index+1)]*900).tobytes()
            path = self.work/(ident+".pcm"); path.write_bytes(raw)
            paths[ident] = path; reference[ident] = BytesIO(raw)
            clip = core.audio_defaults(ident,index*7,600)
            clip.update(source_in=100,source_out=700,fade_in_samples=30,fade_out_samples=75)
            if index%3 == 0: clip.update(duration_samples=1200,loop_offset_samples=193,gain=1.5)
            clips.insert(0,clip)
        opened = []
        with core.AudioMixer(clips,paths,max_open=3) as mixer:
            for cursor,count in ((0,256),(256,128),(1200,30),(15,50),(65,500),(565,10)):
                self.assertEqual(mixer.read(cursor,count),core.mixed_audio_chunk(clips,reference,cursor,count))
                self.assertLessEqual(len(mixer.streams),3)
                opened.extend(mixer.streams.values())
        self.assertTrue(all(stream.closed for stream in opened))

    def test_waveform_cap_preserves_peaks_and_final_partial_bin(self):
        pcm = self.work/"wave.pcm"
        values = array("f",[0.0])*(480*12+13)*2
        values[480*2] = -.7; values[-1] = .9
        pcm.write_bytes(values.tobytes())
        with patch.object(core,"MAX_WAVEFORM_BINS",5):
            bins = core.collect_waveform(pcm,len(values)//2,threading.Event())
            self.assertIsInstance(bins,array)
            self.assertEqual(len(bins),5)
            self.assertAlmostEqual(bins[0],.7,places=6)
            self.assertAlmostEqual(bins[-1],.9,places=6)
            cancel = threading.Event(); cancel.set()
            with self.assertRaisesRegex(core.EditorError,"취소"):
                core.collect_waveform(pcm,len(values)//2,cancel)

    def test_layer_cache_is_bounded_and_invalidated_by_image_change(self):
        project = core.fresh()
        for i in range(8):
            path = self.work/f"image{i}.png"
            Image.new("RGBA",(70,50),(i*25,80,160,150)).save(path)
            project["assets"].append(dict(id=str(i),path=str(path)))
        cue = core.image_defaults("0",0,10)
        project["images"] = [cue]
        with patch.object(core,"SIZE",(64,36)):
            cue["from"].update(x=32,y=18)
            cache = core.LayerCache(64*36*4*2)
            with patch.object(core.Image,"open",wraps=Image.open) as read:
                first = core.render_scene(project,0,cache=cache)
                second = core.render_scene(project,1,cache=cache)
                self.assertIsNone(ImageChops.difference(first,second).getbbox())
                self.assertEqual(read.call_count,1)
            Image.new("RGBA",(71,50),"red").save(project["assets"][0]["path"])
            changed = core.render_scene(project,2,cache=cache)
            self.assertIsNotNone(ImageChops.difference(first,changed).getbbox())
            for i in range(8):
                cue["asset"] = str(i)
                core.render_scene(project,3,cache=cache)
                self.assertLessEqual(cache.bytes,cache.max_bytes)
                self.assertLessEqual(len(cache.items),2)

    def test_jpeg_thumbnail_preserves_rotation_without_full_size_conversion(self):
        path = self.work/"portrait.jpg"
        image = Image.new("RGB",(2400,1600),"red")
        exif = Image.Exif(); exif[274] = 6
        image.save(path,exif=exif)
        with patch.object(Image.Image,"convert",autospec=True,side_effect=Image.Image.convert) as convert:
            result = core.thumbnail_image(path,(96,64))
        self.assertEqual(result.size,(43,64))
        self.assertTrue(all(call.args[0].width <= 96 and call.args[0].height <= 64
                            for call in convert.call_args_list))


class DenseVideoTests(unittest.TestCase):
    def setUp(self):
        ResourceBoundsTests.setUp(self)
        self.ffmpeg, self.ffprobe = bundled_tool("ffmpeg"), bundled_tool("ffprobe")
        if not self.ffmpeg or not self.ffprobe: self.skipTest("FFmpeg required")
        self.source = self.work/"source.mp4"
        self.ff(["-f","lavfi","-i","testsrc2=s=160x90:r=30:d=1","-c:v","libx264","-an",str(self.source)])
        self.project = core.fresh()
        asset = dict(video_media.probe_video(self.source,self.ffprobe),id="v",path=str(self.source))
        self.project["video_assets"] = [asset]
        for index in range(5):
            clip = core.video_defaults("v",0,6)
            clip.update(source_in_frame=index,source_out_frame=index+6,duration_frames=30,end=30,order=index*2)
            clip["from"].update(x=400+index*270,y=350+index*75,zoom=55)
            self.project["videos"].append(clip)
        picture = self.work/"overlay.png"; Image.new("RGBA",(160,90),(0,255,0,90)).save(picture)
        self.project["assets"] = [dict(id="p",path=str(picture))]
        previous = core.image_defaults("p",-6,0)
        incoming = core.image_defaults("p",0,30)
        incoming.update(order=3,transition=dict(type="dissolve",frames=6))
        self.project["images"] = [previous,incoming]
        self.project["texts"] = [dict(id="t",start=0,end=30,text="Test",size=40,width=300,x=960,y=600)]

    def ff(self,args):
        return subprocess.run([self.ffmpeg,"-v","error","-y"]+args,capture_output=True,check=True,
                              timeout=90,creationflags=video_media.FLAGS).stdout

    @patch.object(core,"MAX_COMPOSITE_FRAMES",3)
    def test_dense_export_uses_two_decoders_and_matches_direct_compositor(self):
        processes = []; maximum = [0]
        original = video_media.VideoDecoder._open
        def opened(decoder, number):
            original(decoder,number)
            processes.append(decoder.process)
            maximum[0] = max(maximum[0],sum(p.poll() is None for p in processes))
        output = self.work/"bounded.mp4"
        with patch.object(video_media.VideoDecoder,"_open",autospec=True,side_effect=opened):
            core.export_video(self.project,1,output,self.ffmpeg,threading.Event())
        self.assertLessEqual(maximum[0],2)
        self.assertTrue(all(p.poll() is not None for p in processes))
        @contextmanager
        def direct(project,begin,end,*args):
            yield core.active_visuals(project,begin)
        reference = self.work/"reference.mp4"
        with patch.object(core,"export_scene_layers",side_effect=direct):
            core.export_video(self.project,1,reference,self.ffmpeg,threading.Event())
        def hashes(path):
            raw = self.ff(["-i",str(path),"-map","0:v:0","-f","framemd5","-"])
            return [line for line in raw.splitlines() if not line.startswith(b"#")]
        self.assertEqual(hashes(output),hashes(reference))

    def test_cancel_during_lossless_pass_removes_temporaries(self):
        cancel = threading.Event(); scratch = self.work/"staging"; scratch.mkdir()
        original = core.render_scene
        def render(*args,**kwargs):
            result = original(*args,**kwargs); cancel.set(); return result
        with video_media.VideoFrameProvider(self.project["video_assets"],self.ffmpeg,cancel) as provider:
            with patch.object(core,"render_scene",side_effect=render):
                with self.assertRaisesRegex(core.EditorError,"취소"):
                    with core.export_scene_layers(self.project,0,6,self.ffmpeg,cancel,scratch,provider,core.LayerCache()):
                        self.fail("cancelled composition yielded")
            self.assertFalse(provider.decoders)
            self.assertEqual(set(provider.assets),{"v"})
        self.assertEqual(list(scratch.iterdir()),[])


class ImportQueueTests(unittest.TestCase):
    def setUp(self):
        ResourceBoundsTests.setUp(self)
        self.env = patch.dict(os.environ,LOCALAPPDATA=str(self.work/"settings"))
        self.env.start(); self.addCleanup(self.env.stop)
        self.root = tk.Tk(); self.root.withdraw()
        self.app = EditorApp(self.root); self.app._save_ui_settings = lambda: None
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.addCleanup(self.dispose)
        self.pcm = self.work/"source.pcm"; self.pcm.write_bytes(bytes(core.RATE*8))

    def dispose(self):
        self.app._stop_audio(); self.app.audio_cancel.set(); self.app._dispose_videos()
        self.app.media_executor.shutdown(wait=False,cancel_futures=True)
        for timer in self.root.tk.call("after","info"): self.root.tk.call("after","cancel",timer)
        self.root.destroy()
        self.assertEqual(self.errors,[])

    def settle(self,check):
        deadline = time.monotonic()+10
        while not check() and time.monotonic()<deadline:
            self.root.update(); time.sleep(.005)
        self.assertTrue(check())

    def prepared(self):
        return dict(width=160,height=90,frames=30,video_stream=0,audio_stream=None,
                    has_audio=False,rotation=0,video_start=0,audio_start=0,
                    proxy="unused",thumbnail="unused",bins=array("f"),pcm=None)

    def add(self,kind,index):
        asset = dict(id=f"{kind}{index}",path=str(self.work/f"{kind}{index}"))
        self.app.project["audio_assets" if kind=="audio" else "video_assets"].append(asset)
        (self.app._analyze_asset if kind=="audio" else self.app._prepare_video)(asset)

    def test_audio_and_video_imports_share_one_worker(self):
        active = [0]; peak = [0]; workers = set(); jobs = []
        def process(kind):
            active[0] += 1; peak[0] = max(peak[0],active[0]); workers.add(threading.get_ident())
            try:
                jobs.append(kind); time.sleep(.02)
                return self.prepared() if kind=="video" else (str(self.pcm),array("f",[0]*100),core.RATE)
            finally: active[0] -= 1
        with patch.object(core,"probe_audio",return_value=1), \
                patch.object(core,"analyze_audio_asset",side_effect=lambda *a: process("audio")), \
                patch.object(video_media,"prepare_video",side_effect=lambda *a,**kw: process("video")):
            for index in range(6):
                self.add("audio",index); self.add("video",index)
            self.settle(lambda: not self.app.audio_pending and not self.app.video_pending)
        self.assertEqual(peak[0],1); self.assertEqual(len(workers),1)
        self.assertEqual(jobs,["audio","video"]*6)
        self.assertEqual(len(self.app.audio_cache),6); self.assertEqual(len(self.app.video_cache),6)

    def test_cancelled_waiting_imports_do_not_start_decoding_and_can_retry(self):
        started = threading.Event()
        def video(path,ffmpeg,ffprobe,cancel,**kwargs):
            started.set()
            if not cancel.wait(5): raise AssertionError("cancel was not delivered")
            raise core.EditorError("영상 분석을 취소했습니다.")
        with patch.object(core,"probe_audio",return_value=1), \
                patch.object(core,"analyze_audio_asset",return_value=(str(self.pcm),array("f",[0]*100),core.RATE)) as audio, \
                patch.object(video_media,"prepare_video",side_effect=video) as prepare:
            self.add("video",0); self.assertTrue(started.wait(2))
            self.add("audio",0); self.add("video",1)
            self.app.cancel_job()
            self.settle(lambda: not self.app.audio_pending and not self.app.video_pending)
            self.assertEqual(prepare.call_count,1); self.assertEqual(audio.call_count,0)
            self.app._analyze_asset(self.app.project["audio_assets"][0])
            self.settle(lambda: not self.app.audio_pending)
            self.assertEqual(audio.call_count,1)
            self.assertIn("audio0",self.app.audio_cache)

    def test_deleted_waiting_media_does_not_start_decoding(self):
        started, release = threading.Event(), threading.Event()
        def video(*args, **kwargs):
            started.set()
            if not release.wait(5): raise AssertionError("worker was not released")
            return self.prepared()
        with patch.object(core,"probe_audio",return_value=1) as probe, \
                patch.object(core,"analyze_audio_asset") as audio, \
                patch.object(video_media,"prepare_video",side_effect=video) as prepare:
            try:
                self.add("video",0); self.assertTrue(started.wait(2))
                self.add("audio",0); self.add("video",1)
                for ident in ("audio0", "video1"):
                    self.app.library_selection = ident
                    self.app.delete_media()
            finally: release.set()
            self.app.media_executor.submit(lambda: None).result(timeout=5)
            self.settle(lambda: not self.app.audio_pending and not self.app.video_pending)
            self.assertEqual(prepare.call_count,1)
            probe.assert_not_called(); audio.assert_not_called()
            self.assertEqual(set(self.app.video_cache), {"video0"})

    def test_deleting_running_media_cancels_only_its_import(self):
        for index, kind in enumerate(("audio", "video")):
            with self.subTest(kind=kind):
                started, stopped = threading.Event(), threading.Event()
                ident = f"{kind}{index}"
                def process(path, cancel, result):
                    if Path(path).name == ident:
                        started.set()
                        if not cancel.wait(5): raise AssertionError("deleted job was not cancelled")
                        stopped.set()
                        raise core.EditorError("분석을 취소했습니다.")
                    return result
                with patch.object(core,"probe_audio",return_value=1), \
                        patch.object(core,"analyze_audio_asset",side_effect=lambda path,ffmpeg,cancel:
                                     process(path,cancel,(str(self.pcm),array("f"),core.RATE))), \
                        patch.object(video_media,"prepare_video",side_effect=lambda path,ffmpeg,ffprobe,cancel,**kw:
                                     process(path,cancel,self.prepared())):
                    self.add(kind,index)
                    self.assertTrue(started.wait(2))
                    other = "video" if kind == "audio" else "audio"
                    self.add(other,index+10)
                    self.app.library_selection = ident
                    self.app.delete_media()
                    self.assertTrue(stopped.wait(2))
                    self.app.media_executor.submit(lambda: None).result(timeout=5)
                    self.settle(lambda: not self.app.audio_pending and not self.app.video_pending)
                    cache = self.app.video_cache if other == "video" else self.app.audio_cache
                    self.assertIn(f"{other}{index+10}", cache)
                    self.assertNotIn(ident, self.app.audio_cache)
                    self.assertNotIn(ident, self.app.video_cache)
                    self.assertFalse(self.app.media_job_cancels)


if __name__ == "__main__": unittest.main()
