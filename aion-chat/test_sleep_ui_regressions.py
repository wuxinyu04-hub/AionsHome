"""哄睡 UI 回归护栏。

锁住 2026-07-31 修的三类回归，防止后续改动（含并发 agent）再次悄悄打回：
  1. 故事库入口被 CSS 隐藏 —— 7936e37 曾加 `#openLib{display:none!important}`
  2. 迷你播放器只在 popScreen 里同步 —— playItem 后永远不显示
  3. 书籍列表横滑 + 书名 nowrap 单行截断 —— 用户反馈"书横着放不好读"
另外锁 sleep.js 改动必须 bump sleep.html 的 ?v=（老坑）。
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SLEEP_HTML = ROOT / "static" / "sleep.html"
SLEEP_JS = ROOT / "static" / "sleep.js"


class SleepLibraryEntryTests(unittest.TestCase):
    """故事库入口必须可见可点。"""

    def setUp(self):
        self.html = SLEEP_HTML.read_text(encoding="utf-8")

    def test_library_entry_not_hidden_by_css(self):
        # 允许 openLib 出现在任意选择器里，但不能被 display:none 命中
        for match in re.finditer(r"([^\n{}]*#openLib[^\n{}]*)\{([^}]*)\}", self.html):
            selector, block = match.group(1), match.group(2)
            self.assertNotIn(
                "display:none",
                block.replace(" ", ""),
                f"故事库入口被 CSS 隐藏了（选择器 {selector.strip()!r}）——"
                "迷你播放器进不了故事库，隐藏入口等于砍掉整个功能入口",
            )

    def test_library_entry_button_exists(self):
        self.assertIn('id="openLib"', self.html, "首页必须有故事库入口按钮")

    def test_library_back_not_hidden(self):
        for match in re.finditer(r"([^\n{}]*#libBack[^\n{}]*)\{([^}]*)\}", self.html):
            self.assertNotIn("display:none", match.group(2).replace(" ", ""))


class SleepMiniPlayerTests(unittest.TestCase):
    """迷你播放器必须在切屏时统一同步，而不是只在 popScreen 里。"""

    def setUp(self):
        self.js = SLEEP_JS.read_text(encoding="utf-8")

    def _fn_body(self, name):
        start = self.js.index(f"function {name}(")
        open_pos = self.js.index("{", start)
        depth = 0
        for i in range(open_pos, len(self.js)):
            if self.js[i] == "{":
                depth += 1
            elif self.js[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.js[open_pos + 1:i]
        self.fail(f"function {name} 没闭合")

    def test_show_screen_syncs_mini_player(self):
        # showScreen 是所有切屏的唯一出口；在这里同步才不会漏掉 playItem 路径
        self.assertIn(
            "updateMiniPlayer()",
            self._fn_body("showScreen"),
            "showScreen 必须调用 updateMiniPlayer——否则 playItem 开播后迷你条永远不显示"
            "（7936e37 的原始 bug：只有 popScreen 调过它）",
        )

    def test_mini_player_keyed_on_audio_not_queue(self):
        body = self._fn_body("updateMiniPlayer")
        self.assertIn(
            "audio.src",
            body,
            "显示条件要看 audio.src：队列可能为空但音频在播",
        )
        self.assertNotIn(
            "state.queue.length",
            body,
            "别再用 queue.length 当显示条件——单曲播放时队列为空会误判成没在播",
        )

    def test_mini_player_yields_on_player_screen(self):
        body = self._fn_body("updateMiniPlayer")
        self.assertIn(
            "curScreen",
            body,
            "播放屏有完整控件，迷你条要让位，需要按 curScreen 判断",
        )

    def test_audio_play_pause_resync_mini_player(self):
        # 暂停图标不同步是最容易漏的一环
        for evt in ("'play'", "'pause'"):
            pattern = re.compile(
                r"addEventListener\(\s*" + re.escape(evt) + r"\s*,(.{0,160})",
                re.S,
            )
            match = pattern.search(self.js)
            self.assertIsNotNone(match, f"缺少 audio {evt} 监听")
            self.assertIn(
                "updateMiniPlayer",
                match.group(1),
                f"audio {evt} 事件要同步迷你播放器，否则暂停图标不更新",
            )


class SleepBookLayoutTests(unittest.TestCase):
    """书要竖着排、书名横排正读。"""

    def setUp(self):
        self.html = SLEEP_HTML.read_text(encoding="utf-8")

    def _block(self, selector):
        match = re.search(
            re.escape(selector) + r"\s*\{([^}]*)\}", self.html
        )
        self.assertIsNotNone(match, f"找不到 {selector} 的样式块")
        return match.group(1).replace(" ", "")

    def test_album_row_is_vertical(self):
        block = self._block(".album-row")
        self.assertIn(
            "flex-direction:column",
            block,
            "书库要竖向列表——横滑会把书名压成单行截断（用户反馈'书横着放不好读'）",
        )
        self.assertNotIn("overflow-x:auto", block, "书库不要横滑")

    def test_book_shelf_is_vertical(self):
        block = self._block(".bp-shelf")
        self.assertIn("flex-direction:column", block, "首页选书书架要竖向列表")
        self.assertNotIn("overflow-x:auto", block, "选书书架不要横滑")

    def test_book_titles_wrap_instead_of_truncating(self):
        # 书名允许折行读全，不能是 nowrap 单行省略号
        for selector in (".alb-t", ".bp-book .bt"):
            block = self._block(selector)
            self.assertNotIn(
                "white-space:nowrap",
                block,
                f"{selector} 书名不能 nowrap 单行截断，要能折行读全",
            )

    def test_no_vertical_or_rotated_book_text(self):
        # 用户明确不要竖排文字/旋转文字
        for bad in ("writing-mode:vertical", "writing-mode:tb"):
            self.assertNotIn(
                bad, self.html.replace(" ", ""), "书名不能用竖排文字"
            )


class SleepCacheBustTests(unittest.TestCase):
    """改 sleep.js 必须 bump sleep.html 的 ?v=，否则 F5 拿到旧代码。"""

    def test_script_tag_has_version_query(self):
        html = SLEEP_HTML.read_text(encoding="utf-8")
        self.assertRegex(
            html,
            r"sleep\.js\?v=[\w.-]+",
            "sleep.js 引用必须带 ?v= 缓存版本",
        )

    def test_version_bumped_when_js_changed_in_head_commit(self):
        """HEAD 这一版若改了 sleep.js，同一 commit 必须也动了 sleep.html。"""
        try:
            changed = subprocess.run(
                ["git", "show", "--name-only", "--format=", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            ).stdout
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            self.skipTest(f"git 不可用：{exc}")
        touched_js = "static/sleep.js" in changed
        touched_html = "static/sleep.html" in changed
        if touched_js:
            self.assertTrue(
                touched_html,
                "这个 commit 改了 sleep.js 但没动 sleep.html——"
                "忘了 bump ?v=，浏览器 F5 会继续跑旧代码",
            )


if __name__ == "__main__":
    unittest.main()
