import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import telegram_runtime as rt
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Message


def message(number, text, sender=7, reply=None, private=False, sticker=None):
    values = {"message_id": number, "date": datetime.now(timezone.utc),
              "chat": {"id": sender if private else -1, "type": "private" if private else "supergroup"},
              "from": {"id": sender, "is_bot": False, "first_name": "Tester"}, "text": text}
    if reply:
        values["reply_to_message"] = reply
    if sticker:
        values["sticker"] = sticker
    return Message.model_validate(values)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.error = None

    async def __call__(self, method):
        if self.error:
            error, self.error = self.error, None
            raise error
        self.sent.append(method)
        return SimpleNamespace(message_id=len(self.sent))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def send_chat_action(self, *args, **kwargs):
        return True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = rt.Store(self.path)
        self.bot = FakeBot()
        self.args = SimpleNamespace(chat_id=-1, owner_id=7, mode="all", reply_chance=1,
                                    backend="codex", model="gpt-6-luna")
        self.companion = rt.Companion(self.bot, SimpleNamespace(id=123, username="testbot"),
                                       self.store, self.args)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_legacy_migration_and_search_survive_restart(self):
        self.store.add(1, "legacy:1", "user", "Tester", "Газява нашла секретный хрумтик")
        self.store.add(2, "legacy:2", "user", "Other", "Газява в другой теме")
        found = self.store.search(1, "что нашла газява?", 999)
        self.assertEqual(len(found), 1)
        self.assertIn("хрумтик", found[0]["text"])
        self.store.db.close()
        self.store = rt.Store(self.path)
        self.assertEqual(len(self.store.search(1, "газява", 999)), 1)

    async def test_owner_commands_deny_other_users_and_persist(self):
        await self.companion.receive(message(1, "/pause", sender=9))
        self.assertFalse(self.store.setting("paused", False))
        await self.companion.receive(message(2, "/pause"))
        self.assertTrue(self.store.setting("paused"))
        await self.companion.receive(message(3, "/chance 12"))
        self.assertEqual(self.store.setting("chance"), .12)
        await self.companion.receive(message(4, "/style говори спокойно"))
        self.assertEqual(self.store.setting("owner_style"), "говори спокойно")
        await self.companion.receive(message(5, "/resume"))
        self.assertFalse(self.store.setting("paused"))

    async def test_random_skip_keeps_memory_and_direct_bypasses_chance(self):
        self.store.set_setting("chance", 0)
        with patch.object(rt.random, "random", return_value=.5):
            await self.companion.receive(message(1, "Газява нашла хрумтики"))
            await self.companion.receive(message(2, "@testbot что она нашла?"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(self.companion.queue.qsize(), 1)

    async def test_duplicate_receipt_and_fragment_batching(self):
        first = message(1, "@testbot это первая часть")
        await self.companion.receive(first)
        await self.companion.receive(first)
        await self.companion.receive(message(2, "и продолжение мысли"))
        jobs = self.store.db.execute("SELECT * FROM jobs").fetchall()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["target"], 2)
        self.assertIn("продолжение", jobs[0]["body"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    async def test_context_search_and_concurrent_ingestion(self):
        self.store.add(0, "old:1", "user", "Tester", "Газява любит хрумтики")
        for i in range(2, 28):
            self.store.add(0, f"old:{i}", "user", "Tester", f"Обычная реплика {i}")
        await self.companion.receive(message(100, "@testbot что любит газява?"))
        job = self.store.db.execute("SELECT * FROM jobs").fetchone()
        started, finish = asyncio.Event(), asyncio.Event()

        async def generation(*args):
            started.set()
            await finish.wait()
            return "Газява любит хрумтики."

        self.companion.generate = generation
        self.companion.active_epoch = self.companion.epoch
        task = asyncio.create_task(self.companion.context(job))
        await started.wait()
        await self.companion.receive(message(101, "Новое сообщение во время генерации"))
        self.assertTrue(self.store.seen("user:101"))
        finish.set()
        context = json.loads(await task)
        self.assertIn("Газява", context["memory"])
        self.assertTrue(any("хрумтики" in item["text"] for item in context["recalled"]))
        self.assertFalse(any("во время" in item["text"] for item in context["messages"]))

    async def test_generated_reply_and_tagged_sticker_delivery(self):
        self.store.db.execute("INSERT INTO stickers VALUES('x','file','😼','[\"mystery\"]')")
        self.store.db.commit()
        await self.companion.receive(message(1, "@testbot хихихи"))
        job = self.store.db.execute("SELECT id FROM jobs").fetchone()[0]
        self.companion.active_epoch = self.companion.epoch
        self.companion.generate = AsyncMock(return_value=json.dumps({"text": "Хихихи, сейчас напромптим", "mood": "mystery"}))
        with patch.object(rt.asyncio, "sleep", new=AsyncMock()), patch.object(rt.random, "random", return_value=0):
            await self.companion.process_job(job)
        self.assertEqual([type(item).__name__ for item in self.bot.sent], ["SendMessage", "SendSticker"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE state='sent'").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0], 1)

    def prepare_delivery(self):
        job = self.store.add_job(0, 7, 1, "hello", True)
        row = self.store.add_outbox(job, "text", {"chat_id": -1, "text": "ready"})
        return job, row

    async def test_uncertain_delivery_is_not_automatically_retried(self):
        job, row = self.prepare_delivery()
        self.bot.error = TelegramNetworkError(method=SendMessage(chat_id=-1, text="ready"), message="timeout")
        self.assertEqual(await self.companion.delivery.send(row), "uncertain")
        self.assertEqual(await self.companion.delivery.send(row), "uncertain")
        self.assertEqual(len(self.bot.sent), 0)
        self.assertEqual(self.store.db.execute("SELECT attempts FROM outbox WHERE id=?", (row,)).fetchone()[0], 1)
        await self.companion.receive(message(5, f"/retry {row}"))
        self.assertEqual(self.store.db.execute("SELECT state FROM outbox WHERE id=?", (row,)).fetchone()[0], "pending")

    async def test_rate_limit_retries_and_recovery_from_interruption(self):
        job, row = self.prepare_delivery()
        self.bot.error = TelegramRetryAfter(method=SendMessage(chat_id=-1, text="ready"), message="limit", retry_after=1)
        with patch.object(rt.asyncio, "sleep", new=AsyncMock()):
            self.assertEqual(await self.companion.delivery.send(row), "sent")
        self.store.outbox_state(row, "sending")
        self.store.db.close()
        self.store = rt.Store(self.path)
        self.assertEqual(self.store.db.execute("SELECT state FROM outbox WHERE id=?", (row,)).fetchone()[0], "uncertain")

    async def test_forget_confirm_clears_fts_outbox_and_topic_only(self):
        self.store.add(0, "a", "user", "Tester", "Газява")
        self.store.add(2, "b", "user", "Other", "Хрумтики")
        self.prepare_delivery()
        await self.companion.receive(message(10, "/forget"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages WHERE topic=0").fetchone()[0], 1)
        code = self.companion.forget_codes[(7, 0)][0]
        await self.companion.receive(message(11, "/forget confirm " + code))
        self.assertEqual(self.store.search(0, "газява", 999), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages WHERE topic=2").fetchone()[0], 1)

    async def test_pause_during_generation_prevents_send(self):
        await self.companion.receive(message(1, "@testbot привет"))
        job = self.store.db.execute("SELECT id FROM jobs").fetchone()[0]
        self.companion.active_epoch = self.companion.epoch
        async def generation(*args):
            await self.companion.receive(message(2, "/pause"))
            return '{"text":"should not send","mood":"neutral"}'
        self.companion.generate = generation
        with patch.object(rt.asyncio, "sleep", new=AsyncMock()):
            await self.companion.process_job(job)
        self.assertFalse(any(isinstance(item, SendMessage) for item in self.bot.sent))

    async def test_claim_works_only_in_private_and_only_once(self):
        self.store.db.execute("DELETE FROM settings WHERE key='owner_id'")
        self.store.db.commit()
        code = self.companion.claim_code
        await self.companion.receive(message(1, '/claim ' + code, sender=9))
        self.assertIsNone(self.store.setting('owner_id'))
        await self.companion.receive(message(2, '/claim ' + code, sender=9, private=True))
        self.assertEqual(self.store.setting('owner_id'), 9)
        await self.companion.receive(message(3, '/claim ' + code, sender=10, private=True))
        self.assertEqual(self.store.setting('owner_id'), 9)

    async def test_mood_tags_and_support_override(self):
        sticker = {'file_id':'fake', 'file_unique_id':'unique', 'type':'regular',
                   'width':512, 'height':512, 'is_animated':False, 'is_video':False, 'emoji':'😂'}
        source = message(1, None, sticker=sticker)
        await self.companion.receive(message(2, '/tag поддержка', reply=source))
        self.assertEqual(self.store.choose_sticker('support'), 'fake')
        text, mood = rt.parse_response('{"text":"Держись","mood":"laugh"}', 'мне очень грустно')
        self.assertEqual(mood, 'support')
        self.assertEqual(text, 'Держись')


if __name__ == "__main__":
    unittest.main()
