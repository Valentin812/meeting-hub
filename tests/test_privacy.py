import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyzer  # noqa: E402
import chat  # noqa: E402
import gigachat  # noqa: E402
import kanban_sync  # noqa: E402
import privacy  # noqa: E402

SAMPLE = ("Звонил Иван Петрович Сидоров, телефон +7 916 123-45-67, почта ivan@example.ru. "
          "Счёт 40702810123456789012, сумма 1 250 000 руб., ИНН 7707083893.")


class FakeClient:
    def __init__(self):
        self.sent = []

    def complete(self, messages, model, cancel_event=None):
        self.sent.append(messages)
        return json.dumps({"title": "Звонок", "summary": messages[-1]["content"]}, ensure_ascii=False)

    def stream(self, messages, model, on_piece, cancel_event=None):
        self.sent.append(messages)
        for ch in messages[-1]["content"]:
            on_piece(ch)


class MaskTests(unittest.TestCase):
    def test_masks_and_restores(self):
        m = privacy.Masker()
        out = m.mask(SAMPLE)
        for secret in ("Сидоров", "916", "ivan@example.ru", "40702810123456789012", "7707083893", "1 250 000"):
            self.assertNotIn(secret, out)
        self.assertGreater(m.count, 4)
        self.assertEqual(m.unmask(out), SAMPLE)

    def test_stream_unmasker_handles_split_labels(self):
        m = privacy.Masker()
        out = m.mask(SAMPLE)
        un = privacy.StreamUnmasker(m)
        got = "".join(un.feed(c) for c in out) + un.flush()
        self.assertEqual(got, SAMPLE)


class RouterTests(unittest.TestCase):
    def test_cloud_gets_masked_text_and_log(self):
        client, log = FakeClient(), []
        r = gigachat.LlmRouter("m", client, log=lambda *a: log.append(a))
        res = r.chat_json("sys", SAMPLE, meeting="m1")
        self.assertNotIn("ivan@example.ru", client.sent[0][-1]["content"])
        self.assertEqual(res["summary"], SAMPLE)
        self.assertEqual(log[0][0:2], ("m1", "итоги"))
        self.assertTrue(log[0][4])
        self.assertGreater(log[0][3], 0)

    def test_mask_off(self):
        client = FakeClient()
        gigachat.LlmRouter("m", client, mask=False).chat_json("s", SAMPLE)
        self.assertIn("ivan@example.ru", client.sent[0][-1]["content"])

    def test_local_only_never_touches_cloud(self):
        client = FakeClient()
        r = gigachat.LlmRouter("m", client)
        with mock.patch.object(analyzer, "ollama_chat", return_value={"title": "t"}) as oc:
            self.assertEqual(r.chat_json("s", SAMPLE, local_only=True), {"title": "t"})
        self.assertEqual(client.sent, [])
        oc.assert_called_once()

    def test_stream_masks_and_restores(self):
        client, pieces = FakeClient(), []
        r = gigachat.LlmRouter("m", client)
        answer = r.stream([{"role": "system", "content": "ctx"}, {"role": "user", "content": SAMPLE}],
                          pieces.append, meeting="x")
        self.assertNotIn("ivan@example.ru", json.dumps(client.sent))
        self.assertEqual(answer, SAMPLE)
        self.assertEqual("".join(pieces), SAMPLE)


class FlagTests(unittest.TestCase):
    def test_flags_and_words(self):
        words = privacy.parse_words("Банк, договор")
        self.assertTrue(privacy.is_confidential({"title": "Встреча с банком"}, words))
        self.assertFalse(privacy.is_confidential({"title": "Планёрка"}, words))
        self.assertFalse(privacy.is_confidential({"title": "Встреча с банком", "confidential": False}, words))
        self.assertTrue(privacy.is_confidential({"title": "Планёрка", "confidential": True}, words))

    def test_set_and_stems(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("a", "b"):
                (Path(d) / f"{n}.wav").write_bytes(b"")
                (Path(d) / f"{n}.json").write_text(json.dumps({"title": n}), encoding="utf-8")
            privacy.set_confidential(Path(d) / "a.wav", True)
            self.assertTrue(privacy.wav_confidential(Path(d) / "a.wav"))
            self.assertEqual(set(privacy.confidential_stems(d)), {"a"})

    def test_all_meetings_context_skips(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("a", "b"):
                (Path(d) / f"{n}.summary.json").write_text(
                    json.dumps({"title": "T" + n, "summary": "бюджет " + n}), encoding="utf-8")
            text = chat.all_meetings_context(d, "бюджет", skip=("a",))
            self.assertNotIn("Ta", text.replace("Tab", ""))
            self.assertIn("Tb", text)


class CloudLogTests(unittest.TestCase):
    def test_log_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            lg = privacy.CloudLog(Path(d) / "c.jsonl")
            lg.add("GigaChat", "m1", "итоги", 100, 3)
            lg.add("Канбан", "m1", "итоги", ok=False, error="нет сети")
            self.assertEqual(len(lg.read()), 2)
            self.assertEqual(lg.services_for("m1"), ["GigaChat"])


class KanbanPayloadTests(unittest.TestCase):
    def test_no_transcript_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "2026-01-01_10-00-00.wav"
            wav.write_bytes(b"")
            wav.with_suffix(".txt").write_text("секретный разговор", encoding="utf-8")
            wav.with_suffix(".json").write_text(json.dumps({"title": "T"}), encoding="utf-8")
            (Path(d) / (wav.stem + ".summary.json")).write_text(
                json.dumps({"summary": "итог", "tasks": []}), encoding="utf-8")
            self.assertEqual(kanban_sync.build_payload(wav, with_transcript=False)["transcript"], "")
            self.assertIn("секретный", kanban_sync.build_payload(wav)["transcript"])


if __name__ == "__main__":
    unittest.main()
