"""哄睡 ASMR 剧情 TTS instruction 按 actor 区分。

温叙远(aion)留晚安走剧情演绎（真实起伏）；林叙(connor)留晚安走日常聊天
（松弛不刻意）。actor 已落库（sleep_items.actor），重录/重生成也不会退化。

纯函数测试，不碰 DB/TTS。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bedtime


class SleepActorInstructionTests(unittest.TestCase):
    def test_aion_asmr_uses_drama_instruction(self):
        # 温叙远留晚安：剧情演绎（真实起伏、不要刻意放慢）
        self.assertEqual(bedtime._sleep_tts_instruction("asmr", "aion"), bedtime._DRAMA_INSTRUCTION)

    def test_connor_asmr_uses_daily_instruction(self):
        # 林叙留晚安：返回 ""，tts.py 会构造日常聊天那条（_STEP_DAILY_INSTRUCTION，松弛不刻意）
        self.assertEqual(bedtime._sleep_tts_instruction("asmr", "connor"), "")

    def test_non_asmr_categories_use_no_instruction(self):
        # 普通哄睡（非剧情）不传剧情 instruction，一律走 tts.py 默认（日常慢速）
        for cat in ("boyfriend", "meditation", "reading", "fairy_tale"):
            self.assertEqual(bedtime._sleep_tts_instruction(cat, "aion"), "", cat)
            self.assertEqual(bedtime._sleep_tts_instruction(cat, "connor"), "", cat)

    def test_missing_actor_defaults_to_aion(self):
        # 库里 actor 为空（老条目）按 aion 兜底
        self.assertEqual(bedtime._sleep_tts_instruction("asmr", ""), bedtime._DRAMA_INSTRUCTION)

    def test_actor_name_does_not_leak_into_instruction_text(self):
        # instruction 文本只区分「剧情/非剧情」，不把演员名拼进去
        aion = bedtime._sleep_tts_instruction("asmr", "aion")
        self.assertNotIn("温叙远", aion)
        self.assertNotIn("林叙", aion)


class LeaveAudioPatternTests(unittest.TestCase):
    """对话触发留语音的核心是正则识别 +正文剥离，不生成（_create_leave_audio 落库另测）。"""
    def test_no_topic_match(self):
        m = bedtime.LEAVE_AUDIO_PATTERN.search("[LEAVE_AUDIO]")
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1) or "").strip(), "")

    def test_topic_match(self):
        m = bedtime.LEAVE_AUDIO_PATTERN.search("[LEAVE_AUDIO:海边夜晚]")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), "海边夜晚")

    def test_extracts_topic_and_strips_command_keeping_reply(self):
        # AI 正文是回话，指令在结尾——必须剥掉指令、保留回话
        full = "好呀，给你留一个海边的，弄好了去故事库听。\n[LEAVE_AUDIO:海边夜晚]"
        topic = bedtime.LEAVE_AUDIO_PATTERN.search(full).group(1).strip()
        self.assertEqual(topic, "海边夜晚")
        stripped = bedtime.LEAVE_AUDIO_PATTERN.sub("", full).strip()
        self.assertNotIn("[LEAVE_AUDIO", stripped)
        self.assertIn("好呀", stripped)

    def test_no_match_leaves_text_intact(self):
        full = "我也想留，不过现在不早了明天帮你留吧。"
        self.assertIsNone(bedtime.LEAVE_AUDIO_PATTERN.search(full))

    def test_multiple_commands_in_one_message(self):
        full = "[LEAVE_AUDIO:海边]再看一个[LEAVE_AUDIO:森林]"
        topics = [m.group(1).strip() for m in bedtime.LEAVE_AUDIO_PATTERN.finditer(full)]
        self.assertEqual(topics, ["海边", "森林"])


if __name__ == "__main__":
    unittest.main()
