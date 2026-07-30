"""朋友圈回复可靠性回归测试。

覆盖两个实测 bug：
1. provider 把线路错误当正文 yield，retry 循环形同虚设，错误 JSON 被存成评论。
2. 用户在自己的朋友圈下裸留言（reply_to_id 为空）时，没有任何 AI 被触发。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_providers import looks_like_provider_error


class ProviderErrorDetectionTests(unittest.TestCase):
    def test_detects_tls_handshake_error_blob(self):
        # 实测被存进 moment_comments 的原文
        raw = (
            '{"error":{"message":"Post \\"https://oauth2.googleapis.com/token\\": '
            'net/http: TLS handshake timeout","type":"server_error",'
            '"code":"internal_server_error"}}'
        )
        self.assertTrue(looks_like_provider_error(raw))

    def test_detects_gemini_location_error(self):
        self.assertTrue(looks_like_provider_error(
            '{"error":{"code":400,"message":"User location is not supported '
            'for the API use."}}'
        ))

    def test_detects_bracket_prefixed_cli_errors(self):
        for raw in (
            "[CodexCLI错误 code=1] boom",
            "[GeminiCLI错误] 未找到 gemini CLI",
            "[AntigravityCLI错误] 未登录。",
            "[硅基流动错误 429] rate limited",
            "[错误] 未知模型: nope",
            "[HTTP 500] 请求失败且响应体为空",
        ):
            with self.subTest(raw=raw):
                self.assertTrue(looks_like_provider_error(raw))

    def test_empty_output_counts_as_failure(self):
        self.assertTrue(looks_like_provider_error(""))
        self.assertTrue(looks_like_provider_error("   \n  "))
        self.assertTrue(looks_like_provider_error(None))

    def test_normal_replies_are_not_flagged(self):
        for raw in (
            "拍得真好看，今天心情不错吧？",
            '{"comment":"好看","send_chat_message":false,"chat_message":""}',
            "这也太可爱了吧。[偷笑]",
            "我方括号开头但不是错误：[今天] 天气很好",
        ):
            with self.subTest(raw=raw):
                self.assertFalse(looks_like_provider_error(raw))


class MomentRetryLoopTests(unittest.IsolatedAsyncioTestCase):
    """错误文本必须触发重试，而不是被当成成功回复入库。"""

    def _patches(self, moment, chunk_batches):
        """把 _ai_reply_to_moment 的外部依赖全部挡掉，只留重试逻辑。"""
        calls = {"n": 0}

        async def fake_stream(*args, **kwargs):
            i = min(calls["n"], len(chunk_batches) - 1)
            calls["n"] += 1
            for chunk in chunk_batches[i]:
                yield chunk

        return calls, (
            patch("routes.moments._get_moment_with_comments",
                  new=AsyncMock(return_value=moment)),
            patch("routes.moments._get_recent_context_messages",
                  new=AsyncMock(return_value=[])),
            patch("routes.moments._get_recent_memories",
                  new=AsyncMock(return_value=[])),
            patch("routes.moments._build_moment_reply_messages",
                  return_value=[{"role": "user", "content": "hi"}]),
            patch("routes.moments._prepare_moment_messages_for_model",
                  new=AsyncMock(return_value=[{"role": "user", "content": "hi"}])),
            patch("routes.moments.resolve_model_key", return_value="m"),
            patch("routes.moments.stream_ai", new=fake_stream),
            patch("routes.moments.asyncio.sleep", new=AsyncMock()),
            patch("routes.moments.random.random", return_value=0.99),  # 不点赞
        )

    async def test_error_text_triggers_retry_then_succeeds(self):
        moment = {"id": "mt_1", "author": "connor", "content": "x",
                  "comments": [], "reactions": [], "attachments": []}
        err = ('{"error":{"message":"net/http: TLS handshake timeout",'
               '"code":"internal_server_error"}}')
        calls, patches = self._patches(moment, [[err], ["真正的回复内容"]])

        db = AsyncMock()
        db.__aenter__ = AsyncMock(return_value=db)
        db.__aexit__ = AsyncMock(return_value=False)
        row = {"model": "m"}
        db.execute = AsyncMock(return_value=AsyncMock(
            fetchone=AsyncMock(return_value=row)))

        with (patches[0], patches[1], patches[2], patches[3], patches[4],
              patches[5], patches[6], patches[7], patches[8],
              patch("routes.moments.get_db", return_value=db),
              patch("routes.moments.ws_manager.broadcast", new=AsyncMock())):
            result = await __import__(
                "routes.moments", fromlist=["_ai_reply_to_moment"]
            )._ai_reply_to_moment("aion", "mt_1")

        self.assertEqual(calls["n"], 2, "错误文本必须触发第二次尝试")
        self.assertIsNotNone(result)
        self.assertEqual(result["content"], "真正的回复内容")

    async def test_all_error_attempts_store_nothing(self):
        moment = {"id": "mt_2", "author": "connor", "content": "x",
                  "comments": [], "reactions": [], "attachments": []}
        err = "[GeminiCLI错误] 全挂了"
        calls, patches = self._patches(moment, [[err]])

        db = AsyncMock()
        db.__aenter__ = AsyncMock(return_value=db)
        db.__aexit__ = AsyncMock(return_value=False)
        db.execute = AsyncMock(return_value=AsyncMock(
            fetchone=AsyncMock(return_value={"model": "m"})))
        broadcast = AsyncMock()

        with (patches[0], patches[1], patches[2], patches[3], patches[4],
              patches[5], patches[6], patches[7], patches[8],
              patch("routes.moments.get_db", return_value=db),
              patch("routes.moments.ws_manager.broadcast", broadcast)):
            result = await __import__(
                "routes.moments", fromlist=["_ai_reply_to_moment"]
            )._ai_reply_to_moment("aion", "mt_2")

        self.assertEqual(calls["n"], 3, "应该重试满 3 次")
        self.assertIsNone(result, "3 次都失败不能返回评论")
        broadcast.assert_not_awaited()  # 不能广播错误评论


class CommentRoutingTests(unittest.IsolatedAsyncioTestCase):
    """决定"谁来接话"的路由。核心回归：自己朋友圈下裸留言曾经没人回。"""

    async def _add_comment(self, moment_author, reply_to=None,
                           parent_author=None, participants=()):
        from routes import moments as mod

        rows = {"moment": {"author": moment_author}}
        if reply_to:
            rows["parent"] = {"author": parent_author}

        db = AsyncMock()
        db.__aenter__ = AsyncMock(return_value=db)
        db.__aexit__ = AsyncMock(return_value=False)

        seq = [rows["moment"]] + ([rows["parent"]] if reply_to else [])
        calls = {"i": 0}

        async def execute(sql, params=None):
            cur = AsyncMock()
            if sql.strip().upper().startswith("SELECT"):
                val = seq[calls["i"]] if calls["i"] < len(seq) else None
                calls["i"] += 1
                cur.fetchone = AsyncMock(return_value=val)
            return cur

        db.execute = execute
        db.commit = AsyncMock()

        spawned = {}

        def fake_create_task(coro):
            coro.close()          # 别真的跑 AI
            spawned["called"] = True
            return AsyncMock()

        with (
            patch("routes.moments.get_db", return_value=db),
            patch("routes.moments.ws_manager.broadcast", new=AsyncMock()),
            patch("routes.moments._ai_participants",
                  new=AsyncMock(return_value=list(participants))),
            patch("routes.moments._author_display", side_effect=lambda a: a),
            patch("routes.moments.asyncio.create_task", fake_create_task),
        ):
            body = mod.CommentCreate(content="在吗", reply_to_id=reply_to)
            return await mod.add_comment("mt_1", body)

    async def test_bare_comment_on_own_moment_triggers_last_ai(self):
        """回归主 case：我发朋友圈 → AI 评论 → 我再留言，必须有人接话。"""
        res = await self._add_comment("user", participants=["connor", "aion"])
        self.assertEqual(res["pending_authors"], ["connor"],
                         "应该由最近发言的 AI 接话，而不是没人回")

    async def test_bare_comment_with_no_ai_yet_triggers_nobody(self):
        res = await self._add_comment("user", participants=[])
        self.assertEqual(res["pending_authors"], [])

    async def test_reply_to_ai_comment_targets_that_ai(self):
        res = await self._add_comment("user", reply_to="mc_1",
                                      parent_author="aion",
                                      participants=["connor", "aion"])
        self.assertEqual(res["pending_authors"], ["aion"])

    async def test_comment_on_ai_moment_targets_author(self):
        res = await self._add_comment("connor")
        self.assertEqual(res["pending_authors"], ["connor"])

    async def test_reply_to_own_comment_falls_back_to_participants(self):
        res = await self._add_comment("user", reply_to="mc_u",
                                      parent_author="user",
                                      participants=["aion"])
        self.assertEqual(res["pending_authors"], ["aion"])


if __name__ == "__main__":
    unittest.main()
