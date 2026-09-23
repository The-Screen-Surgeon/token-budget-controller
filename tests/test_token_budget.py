import json
import tempfile
import unittest
from pathlib import Path

from token_budget.adapters import codex_events, claude_events
from token_budget.core import Ledger, UsageEvent

FIXTURES = Path(__file__).parent / "fixtures"


class TokenBudgetTests(unittest.TestCase):
    def test_adapters_extract_usage_without_content(self):
        codex = codex_events(FIXTURES / "codex")
        claude = claude_events(FIXTURES / "claude")
        self.assertEqual(codex[0].total_tokens, 15)
        self.assertEqual((claude[0].input_tokens, claude[0].output_tokens), (7, 3))
        self.assertEqual(claude[0].cache_creation_input_tokens, 4)
        self.assertEqual(claude[0].total_tokens, 16)
        self.assertFalse(hasattr(codex[0], "content"))

    def test_ledger_deduplicates_and_summarizes(self):
        events = codex_events(FIXTURES / "codex")
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "usage.sqlite")
            self.assertEqual(ledger.add_many(events + events), (1, 1))
            summary = ledger.summary()
            self.assertEqual(summary["count"], 1)
            self.assertEqual(summary["total_tokens"], 15)
            ledger.close()

    def test_malformed_or_missing_usage_is_ignored(self):
        path = Path(tempfile.mkdtemp()) / "bad.jsonl"
        records = [
            {"type": "assistant", "message": {}},
            {"type": "assistant", "message": {"usage": {"input_tokens": "bad", "output_tokens": 1}}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 2, "output_tokens": 1}}},
        ]
        path.write_text("not json\n" + "\n".join(json.dumps(record) for record in records))
        events = claude_events(path.parent)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].total_tokens, 3)

    def test_repeated_request_uses_last_valid_usage_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            records = [
                {"type": "assistant", "sessionId": "s1", "requestId": "r1", "message": {"usage": {"input_tokens": 2, "output_tokens": 1}}},
                {"type": "assistant", "sessionId": "s1", "requestId": "r1", "message": {"usage": {"input_tokens": 8, "output_tokens": 5, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1}}},
                {"type": "assistant", "sessionId": "s1", "requestId": "r2", "message": {"usage": {"input_tokens": 3, "output_tokens": 1}}},
                {"type": "assistant", "sessionId": "s1", "message": {"usage": {"input_tokens": 4, "output_tokens": 1}}},
            ]
            path.write_text("\n".join(json.dumps(record) for record in records))
            events = claude_events(directory)
        self.assertEqual(len(events), 3)
        repeated = next(event for event in events if event.event_key == "session:s1:request:r1")
        self.assertEqual(repeated.total_tokens, 16)
        self.assertEqual(repeated.input_tokens, 8)
        self.assertEqual(sum(event.total_tokens for event in events), 25)

    def test_codex_replay_uses_session_ordinal_across_files_and_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "a.jsonl"
            second = Path(directory) / "nested" / "b.jsonl"
            second.parent.mkdir()
            record = {"type": "event_msg", "session_id": "s1", "ordinal": 9,
                      "payload": {"type": "token_count", "info": {"last_token_usage":
                          {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}}}
            first.write_text("noise\n" + json.dumps(record))
            second.write_text("noise\nnoise\n" + json.dumps(record))
            events = codex_events(directory)
            self.assertEqual(len(events), 1)
            ledger = Ledger(Path(directory) / "usage.sqlite")
            self.assertEqual(ledger.add_many(events), (1, 0))
            ledger.close()

    def test_codex_ordinal_reverse_introduction_matches_fresh_scan(self):
        def record(total):
            return {"type": "event_msg", "session_id": "s1", "ordinal": 9,
                    "payload": {"type": "token_count", "info": {"last_token_usage":
                        {"input_tokens": total, "output_tokens": 0, "total_tokens": total}}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.jsonl").write_text(json.dumps(record(9)))
            ledger = Ledger(root / "incremental.sqlite")
            ledger.add_many(codex_events(root))
            (root / "a.jsonl").write_text(json.dumps(record(3)))
            latest = codex_events(root)
            ledger.add_many(latest)
            incremental = ledger.summary()
            ledger.close()
            fresh = Ledger(root / "fresh.sqlite")
            fresh.add_many(latest)
            fresh_summary = fresh.summary()
            fresh.close()
        self.assertEqual(len(latest), 1)
        self.assertEqual(incremental, fresh_summary)
        self.assertEqual(incremental["total_tokens"], 9)

    def test_codex_without_ordinal_uses_cumulative_identity(self):
        def record(total, last_input=2, last_output=1):
            return {"type": "event_msg", "session_id": "s1", "payload": {"type": "token_count",
                "info": {"last_token_usage": {"input_tokens": last_input, "output_tokens": last_output,
                    "cached_input_tokens": 0, "cache_write_input_tokens": 0, "reasoning_output_tokens": 0,
                    "total_tokens": last_input + last_output},
                    "total_token_usage": {"input_tokens": total, "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0, "output_tokens": total, "reasoning_output_tokens": 0,
                        "total_tokens": total * 2}}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.jsonl").write_text(json.dumps(record(3)))
            first_events = codex_events(root)
            (root / "b.jsonl").write_text(json.dumps(record(3)))
            second_events = codex_events(root)
            (root / "c.jsonl").write_text(json.dumps(record(5)))
            all_events = codex_events(root)
            ledger = Ledger(root / "incremental.sqlite")
            ledger.add_many(first_events)
            ledger.add_many(second_events)
            ledger.add_many(all_events)
            incremental_count = ledger.summary()["count"]
            ledger.close()
            fresh = Ledger(root / "fresh.sqlite")
            fresh.add_many(all_events)
            fresh_count = fresh.summary()["count"]
            fresh.close()
        self.assertEqual(len(first_events), 1)
        self.assertEqual(len(second_events), 1)
        self.assertEqual(len(all_events), 2)
        self.assertEqual((incremental_count, fresh_count), (2, 2))

    def test_codex_no_ordinal_missing_auxiliary_defaults_and_cross_file_correction_converges(self):
        def record(last_input, cumulative):
            return {"type": "event_msg", "session_id": "s1", "payload": {"type": "token_count",
                "info": {"last_token_usage": {"input_tokens": last_input, "output_tokens": 1,
                    "total_tokens": last_input + 1},
                    "total_token_usage": {"input_tokens": cumulative, "output_tokens": cumulative,
                        "total_tokens": cumulative * 2}}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.jsonl").write_text("\n" * 99 + json.dumps(record(2, 3)))
            ledger = Ledger(root / "incremental.sqlite")
            ledger.add_many(codex_events(root))
            (root / "b.jsonl").write_text("\n" + json.dumps(record(8, 3)))
            latest = codex_events(root)
            ledger.add_many(latest)
            incremental = ledger.summary()
            ledger.close()
            fresh = Ledger(root / "fresh.sqlite")
            fresh.add_many(latest)
            fresh_summary = fresh.summary()
            fresh.close()
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0].cached_input_tokens, 0)
        self.assertEqual(latest[0].input_tokens, 8)
        self.assertEqual(incremental, fresh_summary)
        self.assertEqual(incremental["total_tokens"], 9)

    def test_codex_nested_relative_path_order_converges(self):
        def record(last_input):
            return {"type": "event_msg", "session_id": "s1", "payload": {"type": "token_count",
                "info": {"last_token_usage": {"input_tokens": last_input, "output_tokens": 1,
                    "total_tokens": last_input + 1}, "total_token_usage": {"input_tokens": 3,
                    "output_tokens": 3, "total_tokens": 6}}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.jsonl").write_text(json.dumps(record(2)))
            ledger = Ledger(root / "incremental.sqlite")
            ledger.add_many(codex_events(root))
            (root / "a").mkdir()
            (root / "a" / "z.jsonl").write_text(json.dumps(record(8)))
            latest = codex_events(root)
            ledger.add_many(latest)
            incremental = ledger.summary()
            ledger.close()
            fresh = Ledger(root / "fresh.sqlite")
            fresh.add_many(latest)
            fresh_summary = fresh.summary()
            fresh.close()
        self.assertEqual(incremental, fresh_summary)
        self.assertEqual(incremental["total_tokens"], 9)

    def test_incremental_claude_ingest_replaces_older_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            base = {"type": "assistant", "sessionId": "s1", "requestId": "r1",
                    "message": {"usage": {"input_tokens": 2, "output_tokens": 1}}}
            path.write_text(json.dumps(base))
            ledger = Ledger(Path(directory) / "usage.sqlite")
            self.assertEqual(ledger.add_many(claude_events(directory)), (1, 0))
            base["message"]["usage"]["output_tokens"] = 7
            path.write_text(path.read_text() + "\n" + json.dumps(base))
            self.assertEqual(ledger.add_many(claude_events(directory)), (1, 0))
            self.assertEqual(ledger.summary()["total_tokens"], 9)
            ledger.close()

    def test_claude_nested_relative_path_order_converges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = {"type": "assistant", "sessionId": "s1", "requestId": "r1",
                     "message": {"usage": {"input_tokens": 2, "output_tokens": 1}}}
            (root / "a.jsonl").write_text(json.dumps(first))
            ledger = Ledger(root / "incremental.sqlite")
            ledger.add_many(claude_events(root))
            second = {"type": "assistant", "sessionId": "s1", "requestId": "r1",
                      "message": {"usage": {"input_tokens": 8, "output_tokens": 5}}}
            (root / "a").mkdir()
            (root / "a" / "z.jsonl").write_text(json.dumps(second))
            latest = claude_events(root)
            ledger.add_many(latest)
            incremental = ledger.summary()
            ledger.close()
            fresh = Ledger(root / "fresh.sqlite")
            fresh.add_many(latest)
            fresh_summary = fresh.summary()
            fresh.close()
        self.assertEqual(incremental, fresh_summary)
        self.assertEqual(incremental["total_tokens"], 13)

    def test_claude_incremental_revision_matches_fresh_sorted_file_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = {"type": "assistant", "sessionId": "s1", "requestId": "r1",
                     "message": {"usage": {"input_tokens": 2, "output_tokens": 1}}}
            first_path = root / "a.jsonl"
            first_path.write_text("\n" * 99 + json.dumps(first))
            ledger = Ledger(root / "incremental.sqlite")
            ledger.add_many(claude_events(root))
            second = {"type": "assistant", "sessionId": "s1", "requestId": "r1",
                      "message": {"usage": {"input_tokens": 8, "output_tokens": 5}}}
            (root / "b.jsonl").write_text(json.dumps(second))
            ledger.add_many(claude_events(root))
            incremental = ledger.summary()
            ledger.close()
            fresh = Ledger(root / "fresh.sqlite")
            fresh.add_many(claude_events(root))
            fresh_summary = fresh.summary()
            fresh.close()
        self.assertEqual(incremental["count"], 1)
        self.assertEqual(incremental["total_tokens"], 13)
        self.assertEqual(incremental, fresh_summary)

    def test_token_counts_require_bounded_python_ints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            bad = [
                {"type": "assistant", "message": {"usage": {"input_tokens": True, "output_tokens": 1}}},
                {"type": "assistant", "message": {"usage": {"input_tokens": -1, "output_tokens": 1}}},
                {"type": "assistant", "message": {"usage": {"input_tokens": 2**63, "output_tokens": 1}}},
                {"type": "assistant", "message": {"usage": {"input_tokens": 2**63 - 2, "output_tokens": 1}}},
            ]
            path.write_text("\n".join(json.dumps(item) for item in bad))
            events = claude_events(directory)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].total_tokens, 2**63 - 1)

    def test_malformed_codex_metadata_and_ordinal_do_not_block_later_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            usage = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}
            records = [
                {"type": "event_msg", "timestamp": {"bad": True}, "ordinal": 1,
                 "payload": {"type": "token_count", "info": {"last_token_usage": usage}}},
                {"type": "event_msg", "timestamp": "ok", "ordinal": 2**63,
                 "payload": {"type": "token_count", "info": {"last_token_usage": usage}}},
                {"type": "event_msg", "timestamp": "ok", "ordinal": 3, "model": {"bad": True},
                 "payload": {"type": "token_count", "info": {"last_token_usage": usage}}},
                {"type": "event_msg", "timestamp": "ok", "ordinal": 4, "model": "model",
                 "payload": {"type": "token_count", "info": {"last_token_usage": usage}}},
            ]
            path.write_text("\n".join(json.dumps(record) for record in records))
            events = codex_events(directory)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].ordinal, 4)

    def test_parser_recovers_from_huge_number_and_deep_nesting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            valid = {"type": "assistant", "sessionId": "s", "message":
                     {"usage": {"input_tokens": 1, "output_tokens": 2}}}
            path.write_text("{" + "\"n\":" + "9" * 5000 + "}\n" +
                            "[" * 1200 + "0" + "]" * 1200 + "\n" + json.dumps(valid))
            events = claude_events(directory)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].total_tokens, 3)

    def test_lone_surrogate_metadata_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            invalid = {"type": "assistant", "timestamp": "\ud800", "message":
                       {"usage": {"input_tokens": 1, "output_tokens": 1}}}
            valid = {"type": "assistant", "timestamp": "ok", "message":
                     {"usage": {"input_tokens": 2, "output_tokens": 1}}}
            path.write_text(json.dumps(invalid) + "\n" + json.dumps(valid))
            events = claude_events(directory)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].total_tokens, 3)

    def test_summary_accumulates_max_int_events_in_python(self):
        max_int = 2**63 - 1
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "usage.sqlite")
            ledger.add(UsageEvent(source="test", occurred_at=None, session_id="a", model=None,
                input_tokens=max_int, output_tokens=0, total_tokens=max_int, event_key="a"))
            ledger.add(UsageEvent(source="test", occurred_at=None, session_id="b", model=None,
                input_tokens=1, output_tokens=0, total_tokens=1, event_key="b"))
            summary = ledger.summary()
            ledger.close()
        self.assertEqual(summary["input_tokens"], max_int + 1)
        self.assertEqual(summary["total_tokens"], max_int + 1)


if __name__ == "__main__":
    unittest.main()
