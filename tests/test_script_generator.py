import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from script_generator import ScriptGenerator


class ScriptGeneratorNarrationValidationTests(unittest.TestCase):
    def _base_payload(self) -> dict:
        return {
            "title": "Test Title",
            "scene_bible": {
                "time_of_day": "dawn",
                "color_anchors": ["gold", "blue"],
                "material_palette": ["stone", "wood"],
                "film_look": "kodak",
                "ambient_sound_base": "wind",
            },
            "caption": "Test caption",
            "sources": [],
            "scenes": [
                {
                    "id": 1,
                    "segment": "HOOK",
                    "duration": 8,
                    "narration": (
                        "This is an intentionally long narration line designed to exceed "
                        "the upper word threshold for an eight second scene in the parser "
                        "so that validation logic is exercised."
                    ),
                    "text_lines": ["line 1"],
                    "emphasis_words": ["line"],
                    "visual_prompt": "A calm courtyard at dawn with soft dust in light.",
                    "camera": "slow dolly in",
                    "color_palette": ["gold", "blue"],
                    "audio_direction": "soft wind and distant birds",
                },
                {
                    "id": 2,
                    "segment": "CORE",
                    "duration": 8,
                    "narration": "Short valid narration line for timing check only.",
                    "text_lines": ["line 2"],
                    "emphasis_words": [],
                    "visual_prompt": "A lantern glowing in stone archways.",
                    "camera": "locked medium shot",
                    "color_palette": ["amber", "gray"],
                    "audio_direction": "quiet ambience",
                },
                {
                    "id": 3,
                    "segment": "RESOLUTION",
                    "duration": 8,
                    "narration": "Another valid narration for continuity and schema validity.",
                    "text_lines": ["line 3"],
                    "emphasis_words": [],
                    "visual_prompt": "A riverbank under warm sunset light.",
                    "camera": "gentle pan",
                    "color_palette": ["orange", "teal"],
                    "audio_direction": "water and breeze",
                },
            ],
        }

    def test_long_narration_is_clamped_instead_of_raising(self):
        generator = ScriptGenerator.__new__(ScriptGenerator)
        response_text = json.dumps(self._base_payload())

        parsed = generator._parse_response(response_text)

        words = parsed["scenes"][0]["narration"].split()
        self.assertLessEqual(len(words), 26)


if __name__ == "__main__":
    unittest.main()
