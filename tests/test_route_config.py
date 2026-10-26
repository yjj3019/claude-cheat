"""routeConfig: the strict YAML-subset settings file, its writer and the legacy .env fallback."""
import importlib.util
import os
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "route_config", Path(__file__).resolve().parents[1] / "routeConfig.py")
cfg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cfg)

VALID = '''# top comment
openrouter:
  model: "vendor/model"   # trailing comment
  api_key: "sk-or-v1-abcdefghijkl"
local:
  url: http://127.0.0.1:11434/v1/messages
  model: 'it''s'
  allow_remote: false
  api_key:
'''


class ParseTests(unittest.TestCase):
    def test_accepts_the_documented_shapes(self):
        doc = cfg.parse(VALID)
        self.assertEqual(doc.errors, [])
        self.assertEqual(doc.sections["openrouter"]["values"],
                         {"model": "vendor/model", "api_key": "sk-or-v1-abcdefghijkl"})
        local = doc.sections["local"]["values"]
        self.assertEqual(local["url"], "http://127.0.0.1:11434/v1/messages")  # ':' and '/' are fine
        self.assertEqual(local["model"], "it's")
        self.assertEqual(local["allow_remote"], "false")  # always a string
        self.assertEqual(local["api_key"], "")

    def test_quotes_comments_and_escapes(self):
        cases = {
            'model: "a # not a comment"': "a # not a comment",
            "model: 'a # not a comment'": "a # not a comment",
            'model: "say \\"hi\\" \\\\ bye"': 'say "hi" \\ bye',
            "model: plain value # comment": "plain value",
            "model: a#b": "a#b",
            'model: ""': "",
            "model:   ": "",
            "model: 한글 모델 🚀": "한글 모델 🚀",
        }
        for line, expected in cases.items():
            doc = cfg.parse("openrouter:\n  " + line + "\n")
            self.assertEqual(doc.sections["openrouter"]["errors"], [], line)
            self.assertEqual(doc.sections["openrouter"]["values"]["model"], expected, line)

    def test_bom_and_crlf_are_accepted(self):
        doc = cfg.parse("\ufeffopenrouter:\r\n  model: \"x\"\r\n")
        self.assertEqual(doc.sections["openrouter"]["values"], {"model": "x"})

    def test_unsupported_syntax_is_rejected_with_a_line_number_and_no_value(self):
        secret = "SECRET-VALUE-123"
        bad = {
            "tab": f"openrouter:\n\tmodel: {secret}\n",
            "anchor": f"openrouter:\n  model: &a {secret}\n",
            "alias": "openrouter:\n  model: *a\n",
            "block": f"openrouter:\n  model: |\n    {secret}\n",
            "folded": "openrouter:\n  model: >\n",
            "list": f"openrouter:\n  - {secret}\n",
            "flow map": f"openrouter:\n  model: {{a: {secret}}}\n",
            "flow list": f"openrouter:\n  model: [{secret}]\n",
            "nested": f"openrouter:\n  model:\n    deeper: {secret}\n",
            "mapping in scalar": f"openrouter:\n  model: a: {secret}\n",
            "no space after colon": f"openrouter:\n  model:{secret}\n",
            "bad indent": f"openrouter:\n    model: {secret}\n",
            "odd indent": f"openrouter:\n model: {secret}\n",
            "unknown key": f"openrouter:\n  modle: {secret}\n",
            "duplicate key": f'openrouter:\n  model: "a"\n  model: "{secret}"\n',
            "duplicate section": f'openrouter:\n  model: "a"\nopenrouter:\n  model: "{secret}"\n',
            "unterminated": f'openrouter:\n  model: "{secret}\n',
            "trailing text": f'openrouter:\n  model: "a" {secret}\n',
            "unknown escape": f'openrouter:\n  model: "\\n{secret}"\n',
            "control char": f'openrouter:\n  model: "a\x01{secret}"\n',
            "line separator": f'openrouter:\n  model: "a\u2028{secret}"\n',
        }
        for name, text in bad.items():
            doc = cfg.parse(text)
            error = doc.first_error("openrouter")
            self.assertIsNotNone(error, name)
            self.assertIsInstance(error.line, int, name)
            self.assertNotIn(secret, str(error), name)

    def test_top_level_garbage_is_a_document_error(self):
        for text in ("model: x\n", "- a\n", "openrouter\n", "{a: 1}\n"):
            doc = cfg.parse(text)
            self.assertTrue(doc.errors, text)
        # and it blocks every service, unlike a section-scoped error
        self.assertIsNotNone(cfg.parse("model: x\nlocal:\n  model: m\n").first_error("local"))

    def test_an_error_in_one_section_does_not_block_the_other(self):
        doc = cfg.parse('openrouter:\n  modle: "x"\nlocal:\n  model: "m"\n')
        self.assertIsNotNone(doc.first_error("openrouter"))
        self.assertIsNone(doc.first_error("local"))

    def test_unknown_sections_are_ignored_whatever_their_syntax(self):
        text = ('openrouter:\n  model: "m"\ncli:\n  kind: codex\n  nested:\n    - a\n    - {b: [1]}\n'
                'local:\n  model: "l"\n')
        doc = cfg.parse(text)
        self.assertEqual(doc.errors, [])
        self.assertEqual(doc.sections["openrouter"]["values"], {"model": "m"})
        self.assertEqual(doc.sections["local"]["values"], {"model": "l"})
        self.assertNotIn("cli", doc.sections)

    def test_unicode_line_separators_do_not_split_lines(self):
        # str.splitlines() would turn the text after U+2028 / U+0085 / U+000B into a phantom line
        for separator in (" ", " ", "\u0085", "\x0b", "\x0c", "\x1c"):
            text = f'# note{separator} junk: here\nopenrouter:\n  model: "m"\n'
            doc = cfg.parse(text)
            self.assertEqual(doc.errors, [], repr(separator))
            self.assertEqual(doc.sections["openrouter"]["values"], {"model": "m"}, repr(separator))
            out = cfg.update_text(text, "openrouter", {"model": "n"})
            self.assertIn(f"# note{separator} junk: here\n", out, repr(separator))

    def test_fuzz_never_raises_and_always_returns_a_document(self):
        rng = random.Random(1234)
        alphabet = list('abcxyz ":\'#-&*|>[]{}\\\t\r\n\u2028é한') + ["openrouter:\n", "local:\n", "  model: "]
        for _ in range(3000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            doc = cfg.parse(text)
            self.assertIsInstance(doc.sections, dict)
            for name in ("openrouter", "local"):
                doc.first_error(name)


class UpdateTextTests(unittest.TestCase):
    def test_updates_in_place_and_keeps_everything_else(self):
        out = cfg.update_text(VALID, "openrouter", {"model": "vendor/new", "api_key": None})
        self.assertIn("# top comment", out)
        self.assertIn('  model: "vendor/new"', out)
        self.assertIn('  api_key: "sk-or-v1-abcdefghijkl"', out)
        self.assertIn("  url: http://127.0.0.1:11434/v1/messages", out)  # other section untouched
        self.assertEqual(cfg.parse(out).sections["openrouter"]["values"]["model"], "vendor/new")

    def test_appends_missing_keys_after_the_last_entry_and_missing_sections_at_the_end(self):
        out = cfg.update_text("openrouter:\n  model: \"m\"\n# between\nlocal:\n  model: \"l\"\n",
                              "openrouter", {"api_key": "sk-or-x"})
        self.assertLess(out.index('  api_key: "sk-or-x"'), out.index("# between"))
        out = cfg.update_text("openrouter:\n  model: \"m\"\n", "local", {"model": "l"})
        self.assertTrue(out.endswith('local:\n  model: "l"\n'))
        self.assertEqual(cfg.parse(out).sections["local"]["values"], {"model": "l"})

    def test_none_for_a_missing_key_adds_nothing_and_an_empty_string_is_written(self):
        base = 'openrouter:\n  model: "m"\n'
        self.assertEqual(cfg.update_text(base, "openrouter", {"api_key": None}), base)
        out = cfg.update_text(base, "openrouter", {"api_key": ""})
        self.assertEqual(cfg.parse(out).sections["openrouter"]["values"]["api_key"], "")

    def test_duplicate_managed_keys_are_collapsed(self):
        out = cfg.update_text('openrouter:\n  model: "a"\n  model: "b"\n', "openrouter", {"model": "c"})
        self.assertEqual(out.count("model:"), 1)

    def test_unknown_sections_and_their_content_survive_byte_for_byte(self):
        future = 'cli:\n  kind: codex\n  extra:\n    - {a: 1}\n'
        out = cfg.update_text('openrouter:\n  model: "m"\n' + future, "openrouter", {"model": "n"})
        self.assertIn(future, out)

    def test_crlf_files_stay_crlf(self):
        out = cfg.update_text('openrouter:\r\n  model: "m"\r\n', "openrouter", {"model": "n"})
        self.assertEqual(out, 'openrouter:\r\n  model: "n"\r\n')

    def test_values_round_trip_through_the_writer(self):
        tricky = ['plain', 'with "quotes"', "back\\slash", "# looks like a comment", "key: value", "- dash",
                  "  padded  ", "true", "false", "", "한글 🚀", "a'b", "http://h:1/p?x=1#f", "&anchor", "*alias",
                  "|block", "[list]", "{map}", "trailing:", "%percent", "@at", "`tick`"]
        for value in tricky:
            out = cfg.update_text("", "openrouter", {"model": value})
            doc = cfg.parse(out)
            self.assertEqual(doc.sections["openrouter"]["errors"], [], value)
            self.assertEqual(doc.sections["openrouter"]["values"]["model"], value, value)

    def test_unwritable_values_are_refused_before_any_text_is_produced(self):
        for value in ("a\nb", "a\x00b", "a\u2028b", "a\u2029b", "a\u0085b", "a\rb", 5, b"bytes"):
            with self.assertRaises(cfg.ConfigError):
                cfg.update_text("", "openrouter", {"model": value})


def make_store(tmp, **files):
    for name, content in files.items():
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        (Path(tmp) / name).write_bytes(data)
    return cfg.Store(Path(tmp) / "openclaude.config.yaml")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())

    def names(self):
        return sorted(p.name for p in Path(self.tmp).iterdir())

    def test_reads_environment_style_names(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": VALID})
        self.assertEqual(store.read_provider("openrouter"),
                         {"OPENROUTER_MODEL": "vendor/model", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-abcdefghijkl"})
        self.assertEqual(store.read_provider("local")["LOCAL_LLM_URL"], "http://127.0.0.1:11434/v1/messages")
        self.assertEqual(store.source("local"), "yaml")

    def test_nothing_configured_reads_as_empty(self):
        store = make_store(self.tmp)
        self.assertEqual(store.read_provider("openrouter"), {})
        self.assertEqual(store.source("openrouter"), "none")

    def test_legacy_file_is_read_when_the_service_has_no_section(self):
        store = make_store(self.tmp, **{".env.openrouter": "OPENROUTER_MODEL=vendor/old\nANTHROPIC_AUTH_TOKEN='sk-or-x'\n"})
        self.assertEqual(store.read_provider("openrouter"),
                         {"OPENROUTER_MODEL": "vendor/old", "ANTHROPIC_AUTH_TOKEN": "sk-or-x"})
        self.assertEqual(store.source("openrouter"), "legacy")

    def test_fallback_is_per_service_so_a_partial_file_does_not_hide_legacy_settings(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": 'local:\n  model: "l"\n',
                                        ".env.openrouter": "OPENROUTER_MODEL=vendor/old\n"})
        self.assertEqual(store.read_provider("local"), {"LOCAL_LLM_MODEL": "l"})
        self.assertEqual(store.read_provider("openrouter"), {"OPENROUTER_MODEL": "vendor/old"})

    def test_a_present_section_is_authoritative_even_when_empty(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": "openrouter:\n",
                                        ".env.openrouter": "OPENROUTER_MODEL=vendor/old\n"})
        self.assertEqual(store.read_provider("openrouter"), {})

    def test_empty_values_read_as_unset(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": 'local:\n  url: ""\n  model: "m"\n  models_url: ""\n'})
        self.assertEqual(store.read_provider("local"), {"LOCAL_LLM_MODEL": "m"})

    def test_the_shipped_example_is_valid_and_configures_nothing(self):
        example = Path(__file__).resolve().parents[1] / "openclaude.config.example.yaml"
        doc = cfg.parse(example.read_text(encoding="utf-8"))
        self.assertEqual(doc.errors, [])
        for name in cfg.SECTIONS:
            self.assertEqual(doc.sections[name]["errors"], [], name)
            # every documented key is present, so users see all of them
            self.assertEqual(set(doc.sections[name]["values"]), set(cfg.SECTIONS[name]["keys"]), name)
        # seeding a first save from it must not make an unconfigured service look configured
        shutil_target = Path(self.tmp) / "openclaude.config.example.yaml"
        shutil_target.write_bytes(example.read_bytes())
        store = cfg.Store(Path(self.tmp) / "openclaude.config.yaml")
        store.write_provider("openrouter", {"OPENROUTER_MODEL": "vendor/m"})
        self.assertEqual(store.read_provider("local"), {"LOCAL_LLM_ALLOW_REMOTE": "false"})
        self.assertEqual(store.read_provider("openrouter"), {"OPENROUTER_MODEL": "vendor/m"})

    def test_error_scope_section_versus_document(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": 'openrouter:\n  modle: "x"\nlocal:\n  model: "m"\n'})
        with self.assertRaises(cfg.ConfigError) as caught:
            store.read_provider("openrouter")
        self.assertEqual((caught.exception.code, caught.exception.line), ("unknown_key", 2))
        self.assertEqual(store.read_provider("local"), {"LOCAL_LLM_MODEL": "m"})
        broken = make_store(self.tmp, **{"openclaude.config.yaml": 'garbage\nlocal:\n  model: "m"\n'})
        with self.assertRaises(cfg.ConfigError):
            broken.read_provider("local")

    def test_a_save_in_one_service_can_repair_a_file_broken_elsewhere(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": 'openrouter:\n  modle: "x"\nlocal:\n  model: "m"\n'})
        store.write_provider("local", {"LOCAL_LLM_MODEL": "n"})
        self.assertEqual(store.read_provider("local"), {"LOCAL_LLM_MODEL": "n"})

    def test_undecodable_settings_file_is_never_rewritten(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": b'openrouter:\n  model: "\xff"\n'})
        before = (Path(self.tmp) / "openclaude.config.yaml").read_bytes()
        for action in (lambda: store.read_provider("openrouter"),
                       lambda: store.write_provider("openrouter", {"OPENROUTER_MODEL": "m"})):
            with self.assertRaises(cfg.ConfigError) as caught:
                action()
            self.assertEqual(caught.exception.code, "config_not_utf8")
        self.assertEqual((Path(self.tmp) / "openclaude.config.yaml").read_bytes(), before)
        self.assertEqual(self.names(), ["openclaude.config.yaml"])

    def test_first_save_starts_from_the_example_and_keeps_its_comments(self):
        example = '# Example\nopenrouter:\n  model: ""   # vendor/model\n  api_key: ""\nlocal:\n  url: ""\n'
        store = make_store(self.tmp, **{"openclaude.config.example.yaml": example})
        store.write_provider("openrouter", {"OPENROUTER_MODEL": "vendor/m", "ANTHROPIC_AUTH_TOKEN": "sk-or-x"})
        text = (Path(self.tmp) / "openclaude.config.yaml").read_text(encoding="utf-8")
        self.assertIn("# Example", text)
        self.assertIn("local:", text)
        self.assertEqual(store.read_provider("openrouter"),
                         {"OPENROUTER_MODEL": "vendor/m", "ANTHROPIC_AUTH_TOKEN": "sk-or-x"})
        self.assertNotIn("openclaude.config.yaml.bak", self.names())  # nothing to back up yet

    def test_first_save_migrates_every_legacy_service_and_leaves_legacy_files_alone(self):
        legacy_or = "ANTHROPIC_AUTH_TOKEN=sk-or-keepme\nOPENROUTER_MODEL=vendor/old\n"
        legacy_local = "LOCAL_LLM_URL=http://127.0.0.1:11434/v1/messages\nLOCAL_LLM_MODEL=llama\n"
        store = make_store(self.tmp, **{".env.openrouter": legacy_or, ".env.local-llm": legacy_local})
        store.write_provider("openrouter", {"OPENROUTER_MODEL": "vendor/new"})
        self.assertEqual(store.read_provider("openrouter"),
                         {"OPENROUTER_MODEL": "vendor/new", "ANTHROPIC_AUTH_TOKEN": "sk-or-keepme"})
        self.assertEqual(store.read_provider("local")["LOCAL_LLM_MODEL"], "llama")
        self.assertEqual((Path(self.tmp) / ".env.openrouter").read_text(), legacy_or)
        self.assertEqual((Path(self.tmp) / ".env.local-llm").read_text(), legacy_local)

    def test_migration_stops_without_creating_a_file_when_a_legacy_file_is_unreadable(self):
        for bad in (b"LOCAL_LLM_API_KEY=\xff\n", b"not a pair\n"):
            tmp = self.enterContext(tempfile.TemporaryDirectory())
            store = make_store(tmp, **{".env.openrouter": "OPENROUTER_MODEL=vendor/old\n", ".env.local-llm": bad})
            with self.assertRaises(cfg.ConfigError):
                store.write_provider("openrouter", {"OPENROUTER_MODEL": "vendor/new"})
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [".env.local-llm", ".env.openrouter"])

    def test_legacy_parsing_keeps_the_launchers_strict_meaning(self):
        store = make_store(self.tmp, **{".env.local-llm": "LOCAL_LLM_API_KEY=abc'\n"})
        self.assertEqual(store.read_provider("local")["LOCAL_LLM_API_KEY"], "abc'")
        store.write_provider("local", {"LOCAL_LLM_MODEL": "m"})
        self.assertEqual(store.read_provider("local")["LOCAL_LLM_API_KEY"], "abc'")

    def test_second_save_keeps_a_backup_of_the_previous_text(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": VALID})
        store.write_provider("openrouter", {"OPENROUTER_MODEL": "vendor/other"})
        backup = (Path(self.tmp) / "openclaude.config.yaml.bak").read_text(encoding="utf-8")
        self.assertEqual(backup, VALID)
        self.assertNotIn("vendor/other", backup)

    def test_unknown_names_are_refused(self):
        store = make_store(self.tmp)
        with self.assertRaises(cfg.ConfigError):
            store.write_provider("openrouter", {"NOT_A_KEY": "x"})
        self.assertEqual(self.names(), [])

    def test_a_failed_replace_keeps_the_original_and_removes_the_staging_file(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": VALID})
        before = (Path(self.tmp) / "openclaude.config.yaml").read_bytes()
        locked = PermissionError(13, "in use")
        locked.winerror = 32
        with mock.patch.object(cfg.os, "replace", side_effect=locked) as replace, \
                mock.patch.object(cfg.time, "sleep"):
            with self.assertRaises(OSError):
                store.write_provider("openrouter", {"OPENROUTER_MODEL": "vendor/other"})
        self.assertEqual(replace.call_count, 4)  # retried, then gave up
        self.assertEqual((Path(self.tmp) / "openclaude.config.yaml").read_bytes(), before)
        self.assertFalse([n for n in self.names() if n.endswith(".tmp")])

    def test_a_transient_sharing_violation_is_retried(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": VALID})
        real = os.replace
        calls = []

        def flaky(source, target):
            calls.append(1)
            if len(calls) < 3:
                error = PermissionError(13, "in use")
                error.winerror = 32
                raise error
            return real(source, target)

        with mock.patch.object(cfg.os, "replace", side_effect=flaky), mock.patch.object(cfg.time, "sleep"):
            store.write_provider("openrouter", {"OPENROUTER_MODEL": "vendor/other"})
        self.assertEqual(store.read_provider("openrouter")["OPENROUTER_MODEL"], "vendor/other")

    def test_concurrent_saves_to_different_services_do_not_lose_each_other(self):
        store = make_store(self.tmp, **{"openclaude.config.yaml": VALID})
        errors = []

        def hammer(provider, name):
            try:
                for index in range(40):
                    store.write_provider(provider, {name: f"value-{index}"})
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=hammer, args=("openrouter", "OPENROUTER_MODEL")),
                   threading.Thread(target=hammer, args=("local", "LOCAL_LLM_MODEL"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(store.read_provider("openrouter")["OPENROUTER_MODEL"], "value-39")
        self.assertEqual(store.read_provider("local")["LOCAL_LLM_MODEL"], "value-39")
        self.assertFalse([n for n in self.names() if n.endswith(".tmp")])
        # the untouched keys survived every write
        self.assertEqual(store.read_provider("openrouter")["ANTHROPIC_AUTH_TOKEN"], "sk-or-v1-abcdefghijkl")


try:
    import yaml  # optional: only used to prove the subset is also valid YAML, never imported by the app
except ImportError:  # pragma: no cover
    yaml = None


@unittest.skipUnless(yaml, "PyYAML is not installed")
class StandardYamlAgreementTests(unittest.TestCase):
    """The subset must mean the same thing to a real YAML parser, so hand edits and tools agree."""

    @staticmethod
    def normal(value):
        return str(value).lower() if isinstance(value, bool) else ("" if value is None else str(value))

    def test_valid_documents_load_identically(self):
        for text in (VALID,):
            ours = {name: s["values"] for name, s in cfg.parse(text).sections.items()}
            theirs = {name: {k: self.normal(v) for k, v in (body or {}).items()}
                      for name, body in yaml.safe_load(text).items()}
            self.assertEqual(ours, theirs)

    def test_writer_output_loads_identically(self):
        values = ["plain", 'with "quotes"', "back\\slash", "# not a comment", "key: value", "- dash", "true",
                  "", "한글 🚀", "a'b", "http://h:1/p", "&anchor", "*alias", "|block", "[list]", "{map}", "%x", "@x"]
        for value in values:
            text = cfg.update_text("", "openrouter", {"model": value, "api_key": "sk-or-1"})
            self.assertEqual(self.normal(yaml.safe_load(text)["openrouter"]["model"]), value, value)


if __name__ == "__main__":
    unittest.main()
