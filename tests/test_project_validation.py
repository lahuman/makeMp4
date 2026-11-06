"""Invalid project files must fail before replacing editor state."""
import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import editor_core as core


class ProjectValidationTests(unittest.TestCase):
    def load(self, data):
        with patch.object(Path, "read_text", return_value=json.dumps(data)):
            return core.load_project("project.json")

    def project(self):
        p = core.fresh()
        p["assets"] = [dict(id="i", path="photo.png")]
        p["images"] = [core.image_defaults("i", 0, 30)]
        p["audio_assets"] = [dict(id="a", path="music.wav", samples=48000)]
        p["audio_clips"] = [core.audio_defaults("a", 0, 48000)]
        return p

    def test_invalid_json_shapes_and_required_keys(self):
        for data in ([], None, 3, dict(core.fresh(), images=[3]),
                     {"version": 3, "assets": [], "images": [], "texts": []}):
            with self.subTest(data=data), self.assertRaises(core.EditorError): self.load(data)

    def test_invalid_references_ids_and_numbers(self):
        for edit in (lambda p: p["images"][0].update(asset="missing"),
                     lambda p: p["audio_clips"][0].update(source_out=48001),
                     lambda p: p["assets"].append(copy.deepcopy(p["assets"][0])),
                     lambda p: p["images"][0]["from"].update(x=float("nan")),
                     lambda p: p["images"][0].update(start=True),
                     lambda p: p["images"][0].update(transition=None)):
            p = self.project(); edit(p)
            with self.subTest(edit=edit), self.assertRaises(core.EditorError): self.load(p)

    def test_nested_split_transition_and_v2_roundtrip(self):
        p = self.project(); p["version"] = 2
        p["images"][0]["transition"] = dict(type="dissolve", frames=15, offset_frames=4,
            source={k:v for k,v in core.image_defaults("i",0,15).items() if k != "transition"})
        loaded = self.load(p)
        self.assertEqual(loaded["version"], 3)
        self.assertEqual(loaded["images"][0]["transition"], p["images"][0]["transition"])

    def test_missing_image_reference_raises_instead_of_rendering_black(self):
        p = core.fresh()
        with self.assertRaisesRegex(core.EditorError, "참조"):
            core.image_layer(p, core.image_defaults("missing", 0, 30), 0)

    def test_legacy_zero_duration_audio_can_be_reprobed(self):
        p = dict(version=1, audio="music.wav", assets=[], images=[], texts=[])
        loaded = self.load(p)
        self.assertTrue(loaded["_migrated_v1"])
        self.assertEqual(loaded["audio_assets"][0]["samples"], 0)

    def test_invalid_save_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"project.json";core.save_project(self.project(),path)
            previous=path.read_bytes()
            invalid=self.project();invalid["images"][0]["asset"]="missing"
            with self.assertRaises(core.EditorError): core.save_project(invalid,path)
            self.assertEqual(path.read_bytes(),previous)
