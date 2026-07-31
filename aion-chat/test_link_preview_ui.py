import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class LinkPreviewUiTests(unittest.TestCase):
    def test_main_chat_renders_clickable_link_preview_cards(self):
        js = (ROOT / "static" / "chat.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "chat.css").read_text(encoding="utf-8")
        html = (ROOT / "static" / "chat.html").read_text(encoding="utf-8")

        self.assertIn("type === 'link_preview'", js)
        self.assertIn("buildLinkPreviewCard", js)
        self.assertIn("openExternalLink", js)
        self.assertIn("AionExternal", js)
        self.assertIn('target="_blank"', js)
        self.assertIn('rel="noopener noreferrer"', js)
        self.assertIn(".link-preview-card", css)
        self.assertIn(".msg-row:not(.user) .msg-media", css)
        self.assertIn("align-self:flex-start", css)
        self.assertIn("--user-link-preview-offset", css)
        self.assertIn("margin-right:var(--user-link-preview-offset)", css)
        self.assertNotIn("padding-right:var(--user-link-preview-offset)", css)
        # 只保证带缓存版本，不钉死具体版本号（否则每次 bump 都要改测试）
        self.assertRegex(html, r"chat\.css\?v=[\w.-]+")

    def test_chatroom_renders_clickable_link_preview_cards(self):
        js = (ROOT / "static" / "chatroom.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "chatroom.css").read_text(encoding="utf-8")
        html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")

        self.assertIn("type === 'link_preview'", js)
        self.assertIn("crBuildLinkPreviewCard", js)
        self.assertIn("crOpenExternalLink", js)
        self.assertIn("AionExternal", js)
        self.assertIn('target="_blank"', js)
        self.assertIn('rel="noopener noreferrer"', js)
        self.assertIn(".link-preview-card", css)
        self.assertIn(".message-row:not(.user) .msg-media", css)
        self.assertIn("align-self: flex-start", css)
        self.assertIn("--user-link-preview-offset", css)
        self.assertIn("--chatroom-message-avatar-size: 28px", css)
        self.assertIn("--chatroom-message-avatar-gap: 6px", css)
        self.assertIn("--user-link-preview-offset: calc(var(--chatroom-message-avatar-size) + var(--chatroom-message-avatar-gap))", css)
        self.assertIn("gap: var(--chatroom-message-avatar-gap)", css)
        self.assertIn("width: var(--chatroom-message-avatar-size)", css)
        self.assertIn("margin-right: var(--user-link-preview-offset)", css)
        self.assertRegex(html, r"chatroom\.css\?v=[\w.-]+")


if __name__ == "__main__":
    unittest.main()
