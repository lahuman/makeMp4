"""Hardware selection must preserve the output contract and CPU recovery."""
from array import array
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

import editor_core as core
from seonyuldam import bundled_tool


class GPUSelectionTests(unittest.TestCase):
    def test_cpu_does_not_probe_and_forwards_quality(self):
        project = core.fresh()
        with patch.object(core, "probe_gpu_encoder") as probe, patch.object(core, "_export_video", return_value="done") as export:
            self.assertEqual(core.export_video(project,1,"unused.mp4","ffmpeg",threading.Event(), quality="최고화질"),"done")
            probe.assert_not_called()
            self.assertEqual(export.call_args.args[-2:],("최고화질","libx264"))

    def test_failed_gpu_restarts_cpu_and_keeps_project(self):
        project = core.fresh(); original = copy.deepcopy(project); notices = []
        with patch.object(core, "probe_gpu_encoder", return_value="h264_qsv"), \
                patch.object(core,"_export_video",side_effect=[core.EncoderError("device failed"),"done"]) as export:
            self.assertEqual(core.export_video(project,1,"unused.mp4","ffmpeg",threading.Event(), acceleration="auto",on_encoder=notices.append),"done")
        self.assertEqual([c.args[-1] for c in export.call_args_list],["h264_qsv","libx264"])
        self.assertIn("CPU",notices[-1]); self.assertEqual(project,original)

    def test_no_supported_gpu_uses_cpu_without_hardware_export_attempt(self):
        notices=[]
        with patch.object(core,"probe_gpu_encoder",return_value=None),patch.object(core,"_export_video",return_value="done") as export:
            core.export_video(core.fresh(),1,"unused.mp4","ffmpeg",threading.Event(),acceleration="auto",on_encoder=notices.append)
            self.assertEqual(export.call_count,1);self.assertEqual(export.call_args.args[-1],"libx264")
            self.assertIn("CPU",notices[-1])

    def test_missing_media_and_cancellation_do_not_trigger_retry(self):
        for error, cancelled in ((core.EditorError("missing media"),False),(core.EncoderError("stopped"),True)):
            cancel = threading.Event()
            def fail(*args):
                if cancelled: cancel.set()
                raise error
            with patch.object(core,"probe_gpu_encoder",return_value="h264_qsv"), patch.object(core,"_export_video",side_effect=fail) as export:
                with self.assertRaises(core.EditorError):
                    core.export_video(core.fresh(),1,"unused.mp4","ffmpeg",cancel,acceleration="auto")
                self.assertEqual(export.call_count,1)

    def test_invalid_acceleration_and_collision_are_rejected_before_probe(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(core,"probe_gpu_encoder") as probe:
            path=Path(folder)/"output.mp4";path.write_bytes(b"existing")
            with self.assertRaises(core.EditorError):core.export_video(core.fresh(),1,path,"ffmpeg",threading.Event(),acceleration="auto")
            self.assertEqual(path.read_bytes(),b"existing")
            with self.assertRaises(core.EditorError):core.export_video(core.fresh(),1,Path(folder)/"other.mp4","ffmpeg",threading.Event(),acceleration="invalid")
            probe.assert_not_called()

    def test_cancelled_probe_does_not_start_a_process(self):
        cancel=threading.Event();cancel.set()
        with patch.object(core.subprocess,"Popen") as process:
            with self.assertRaisesRegex(core.EditorError,"취소"):core.probe_gpu_encoder("ffmpeg",(1920,1080),core.DEFAULT_EXPORT_QUALITY,cancel)
            process.assert_not_called()

    def test_probe_timeout_terminates_each_driver_process(self):
        with patch.object(core.subprocess,"Popen") as start, \
                patch.object(core.time,"monotonic",side_effect=[0,11,20,31,40,51]), \
                patch.object(threading.Event,"wait",return_value=False):
            start.return_value.poll.return_value=None
            self.assertIsNone(core.probe_gpu_encoder("ffmpeg",(1920,1080),core.DEFAULT_EXPORT_QUALITY,threading.Event()))
            self.assertEqual(start.return_value.terminate.call_count,3)
            self.assertEqual(start.return_value.wait.call_count,3)

    def test_probe_cancellation_terminates_running_driver_process(self):
        cancel=threading.Event()
        with patch.object(core.subprocess,"Popen") as start, patch.object(cancel,"wait",return_value=True):
            start.return_value.poll.return_value=None
            with self.assertRaisesRegex(core.EditorError,"취소"):
                core.probe_gpu_encoder("ffmpeg",(1920,1080),core.DEFAULT_EXPORT_QUALITY,cancel)
            start.return_value.terminate.assert_called_once()


class GPUExportTests(unittest.TestCase):
    def setUp(self):
        self.ffmpeg,self.ffprobe=bundled_tool("ffmpeg"),bundled_tool("ffprobe")
        if not self.ffmpeg or not self.ffprobe:self.skipTest("FFmpeg/FFprobe required")
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.work=Path(self.folder.name)
        image=self.work/"image.png";Image.new("RGB",(160,90),(35,110,190)).save(image)
        pcm=self.work/"music.pcm";pcm.write_bytes(array("f",[.03,-.03]*48000).tobytes())
        self.project=core.fresh();self.project["assets"]=[dict(id="i",path=str(image))]
        self.project["images"]=[core.image_defaults("i",0,15,(160,90)),core.image_defaults("i",15,30,(160,90))]
        self.project["images"][1]["motion"]=True
        self.project["audio_assets"]=[dict(id="a",path=str(pcm),samples=48000)]
        self.project["audio_clips"]=[core.audio_defaults("a",0,48000)]
        self.paths={"a":pcm}

    def verify(self,path):
        metadata=json.loads(subprocess.check_output([self.ffprobe,"-v","error","-show_streams","-of","json",str(path)]))
        video=next(s for s in metadata["streams"] if s["codec_type"]=="video")
        self.assertEqual((video["codec_name"],video["nb_frames"],video["width"],video["height"]),("h264","30",160,90))
        for key in ("color_space","color_transfer","color_primaries"):self.assertEqual(video[key],"bt709")
        self.assertTrue(any(s["codec_name"]=="aac" for s in metadata["streams"]))
        subprocess.run([self.ffmpeg,"-v","error","-i",str(path),"-f","null","-"],check=True,capture_output=True,timeout=30)

    def test_real_auto_export_decodes_across_static_motion_boundary(self):
        output=self.work/"auto.mp4";notices=[]
        with patch.object(core,"project_size",return_value=(160,90)):
            core.export_video(self.project,1,output,self.ffmpeg,threading.Event(),pcm_paths=self.paths,acceleration="auto",on_encoder=notices.append)
        self.verify(output);self.assertTrue(notices)

    def test_gpu_failure_discards_attempt_before_real_cpu_output(self):
        output=self.work/"recovered.mp4";original=core._run_encoder;failed=[]
        def encode(command,*args,**kwargs):
            if "h264_qsv" in command:
                failed.append(command)
                Path(command[-1]).write_bytes(b"partial hardware attempt")
                raise core.EncoderError("injected device loss")
            return original(command,*args,**kwargs)
        with patch.object(core,"project_size",return_value=(160,90)), \
                patch.object(core,"probe_gpu_encoder",return_value="h264_qsv"),patch.object(core,"_run_encoder",side_effect=encode):
            core.export_video(self.project,1,output,self.ffmpeg,threading.Event(),pcm_paths=self.paths,acceleration="auto")
        self.verify(output);self.assertEqual(len(failed),1)
        self.assertFalse(Path(failed[0][-1]).exists());self.assertEqual(list(self.work.glob(".*.tmp.mp4")),[])


if __name__=="__main__":unittest.main()
