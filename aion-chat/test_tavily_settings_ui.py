import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class TavilySettingsUiTests(unittest.TestCase):
    def test_settings_page_exposes_and_saves_tavily_key(self):
        html = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")

        self.assertIn('id="tavilyApiKeyInput"', html)
        self.assertIn("Tavily API Key", html)
        self.assertIn("s.tavily_api_key", html)
        self.assertIn('tavily_api_key: $("tavilyApiKeyInput").value.trim()', html)

    def test_personal_data_cleanup_wipes_all_api_keys(self):
        """清理脚本必须整体重置 settings.json。

        原断言是"脚本里出现 Tavily 字样"，但脚本已改成 `echo {} > settings.json`
        整体清空，逐个 key 点名反而是更弱、且每加一个 provider 就会漏的保证；
        脚本本身也从仓库根移到了 ops/install/。这里改成断言那条整体重置命令，
        既跟着文件走，也自动覆盖以后新增的任何 key。
        """
        candidates = list(ROOT.parent.rglob("清理个人数据.bat"))
        self.assertTrue(candidates, "找不到 清理个人数据.bat")
        cleanup_bat = candidates[0].read_text(encoding="utf-8")

        self.assertIn('echo {} > "aion-chat\\data\\settings.json"', cleanup_bat)


if __name__ == "__main__":
    unittest.main()
