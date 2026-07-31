import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class ChatroomMobileFocusTests(unittest.TestCase):
    def test_post_send_focus_is_guarded_on_touch_devices(self):
        js = (ROOT / "static" / "chatroom.js").read_text(encoding="utf-8")
        html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")

        self.assertIn("function crShouldAutoFocusComposer()", js)
        self.assertIn("matchMedia('(pointer: coarse)')", js)
        self.assertIn("navigator.maxTouchPoints", js)
        self.assertIn("function crRefocusComposerAfterSend()", js)
        # 原来钉死 'chatroom-mobile-focus-20260704'，生产 bump 到 -20260715 就红。
        # 真正要保证的是"引用带缓存版本"，不是某个具体版本号。
        self.assertRegex(
            html,
            r"chatroom\.js\?v=[\w.-]+",
            "chatroom.js 引用必须带 ?v= 缓存版本",
        )

        voice_call_send = re.search(
            r"window\.ChatroomVoiceCallAdapter = \{.*?async sendText\(text\).*?\n  \}\n\};",
            js,
            re.S,
        )
        self.assertIsNotNone(voice_call_send)
        self.assertNotIn("inputEl?.focus()", voice_call_send.group(0))
        self.assertIn("crRefocusComposerAfterSend();", voice_call_send.group(0))

        composer_submit = re.search(
            r"composer\.addEventListener\('submit', async \(e\) => \{.*?\n\}\);",
            js,
            re.S,
        )
        self.assertIsNotNone(composer_submit)
        self.assertNotIn("inputEl.focus();", composer_submit.group(0))
        self.assertIn("crRefocusComposerAfterSend();", composer_submit.group(0))

        voice_send = re.search(r"async function _crVoiceSend\(audioBlob, duration\).*?\n\}", js, re.S)
        self.assertIsNotNone(voice_send)
        self.assertNotIn("inputEl.focus();", voice_send.group(0))
        self.assertIn("crRefocusComposerAfterSend();", voice_send.group(0))


if __name__ == "__main__":
    unittest.main()
