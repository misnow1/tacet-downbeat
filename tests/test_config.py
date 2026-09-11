"""The optional config file, and the promises it makes.

The one that matters most is precedence: built-in default < config < flag. An
operator who passes `--dca 4` on top of a config saying 3 must drive DCA 4, and
that is asserted here rather than assumed of argparse.

The rest is about failing visibly. A config file is a new way for the box to be
wrong quietly -- a misspelled key, a port that is a string, a path that was
never there -- so every one of those is an error with the file named in it.
"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tacet import config, serve, verify_dm7, verify_reaper

SAMPLE = """
[console]
host = "10.0.0.5"
port = 49900
dca = 3
quantized = true

[reaper]
host = "127.0.0.1"
send_port = 8000
receive_port = 9000

[capture]
log = "~/games/2026-09-13.jsonl"
queue = "~/queue.tsv"

[fader]
fade_seconds = 2.5

[ui]
listen = "0.0.0.0"
port = 8080
"""


class _TempConfig(unittest.TestCase):
    """Writes config files into a directory that goes away with the test."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def write(self, text, name=config.CONFIG_FILENAME):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path


class TestValuesFromMapping(unittest.TestCase):
    def test_a_full_document_flattens_to_dotted_names(self):
        values = config.values_from_mapping({"console": {"host": "10.0.0.5", "dca": 3}})
        self.assertEqual(values, {"console.host": "10.0.0.5", "console.dca": 3})

    def test_an_unknown_section_names_the_sections_that_exist(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.values_from_mapping({"consoel": {"host": "x"}})
        self.assertIn("consoel", str(caught.exception))
        self.assertIn("console", str(caught.exception))

    def test_an_unknown_key_names_the_keys_that_exist(self):
        # The failure this whole check exists for: a config that looks right,
        # sets nothing, and leaves the tool on its built-in default.
        with self.assertRaises(config.ConfigError) as caught:
            config.values_from_mapping({"console": {"dca_number": 3}})
        self.assertIn("dca_number", str(caught.exception))
        self.assertIn("dca", str(caught.exception))

    def test_a_section_that_is_not_a_table_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.values_from_mapping({"console": "10.0.0.5"})

    def test_a_string_where_a_number_belongs_is_rejected(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.values_from_mapping({"console": {"port": "49900"}})
        self.assertIn("console.port", str(caught.exception))

    def test_a_bool_is_not_accepted_as_a_number(self):
        # True == 1 in Python. `port = true` must not become port 1.
        with self.assertRaises(config.ConfigError):
            config.values_from_mapping({"console": {"port": True}})

    def test_a_number_is_not_accepted_as_a_bool(self):
        with self.assertRaises(config.ConfigError):
            config.values_from_mapping({"console": {"quantized": 1}})

    def test_a_whole_number_is_accepted_for_a_float(self):
        values = config.values_from_mapping({"fader": {"fade_seconds": 2}})
        self.assertEqual(values["fader.fade_seconds"], 2.0)
        self.assertIsInstance(values["fader.fade_seconds"], float)

    def test_a_path_is_expanded_and_becomes_a_path(self):
        values = config.values_from_mapping({"capture": {"log": "~/games/x.jsonl"}})
        log = values["capture.log"]
        self.assertIsInstance(log, Path)
        self.assertNotIn("~", str(log))

    def test_the_error_names_the_file(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.values_from_mapping({"nope": {}}, where="/etc/tacet.toml")
        self.assertIn("/etc/tacet.toml", str(caught.exception))


class TestLoad(_TempConfig):
    def test_a_whole_file_round_trips(self):
        values = config.load(self.write(SAMPLE))
        self.assertEqual(values["console.host"], "10.0.0.5")
        self.assertEqual(values["console.dca"], 3)
        self.assertIs(values["console.quantized"], True)
        self.assertEqual(values["reaper.send_port"], 8000)
        self.assertEqual(values["fader.fade_seconds"], 2.5)
        self.assertEqual(values["ui.port"], 8080)

    def test_broken_toml_is_reported_with_the_path(self):
        path = self.write("[console\nhost = 'x'")
        with self.assertRaises(config.ConfigError) as caught:
            config.load(path)
        self.assertIn(str(path), str(caught.exception))

    def test_a_missing_file_is_an_error_not_an_empty_config(self):
        with self.assertRaises(config.ConfigError):
            config.load(self.dir / "absent.toml")

    def test_an_empty_file_is_a_valid_empty_config(self):
        self.assertEqual(config.load(self.write("")), {})


class TestDiscover(_TempConfig):
    def test_an_explicit_path_that_is_missing_is_an_error(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.discover(self.dir / "absent.toml", environ={}, search=[])
        self.assertIn(config.CONFIG_FLAG, str(caught.exception))

    def test_an_environment_path_that_is_missing_is_an_error(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.discover(None, environ={config.CONFIG_ENV_VAR: str(self.dir / "absent.toml")}, search=[])
        self.assertIn(config.CONFIG_ENV_VAR, str(caught.exception))

    def test_the_flag_beats_the_environment(self):
        flagged = self.write(SAMPLE, "flagged.toml")
        env = self.write(SAMPLE, "env.toml")
        found = config.discover(flagged, environ={config.CONFIG_ENV_VAR: str(env)}, search=[])
        self.assertEqual(found, flagged)

    def test_the_environment_beats_the_search_path(self):
        env = self.write(SAMPLE, "env.toml")
        searched = self.write(SAMPLE, "searched.toml")
        found = config.discover(None, environ={config.CONFIG_ENV_VAR: str(env)}, search=[searched])
        self.assertEqual(found, env)

    def test_the_search_path_is_taken_in_order(self):
        second = self.write(SAMPLE, "second.toml")
        found = config.discover(None, environ={}, search=[self.dir / "absent.toml", second])
        self.assertEqual(found, second)

    def test_no_config_anywhere_is_not_an_error(self):
        self.assertIsNone(config.discover(None, environ={}, search=[self.dir / "absent.toml"]))

    def test_the_default_search_is_cwd_then_home(self):
        paths = config.default_search_paths(cwd=Path("/work"), home=Path("/home/me"))
        self.assertEqual(paths[0], Path("/work") / config.CONFIG_FILENAME)
        self.assertEqual(paths[1], Path("/home/me") / config.USER_CONFIG_DIR / config.CONFIG_FILENAME)


class TestDefaultsFor(unittest.TestCase):
    def test_only_keys_present_in_the_config_are_returned(self):
        defaults = config.defaults_for({"dca": "console.dca", "fade": "fader.fade_seconds"}, {"console.dca": 3})
        self.assertEqual(defaults, {"dca": 3})

    def test_a_mapping_onto_a_key_that_does_not_exist_is_a_bug_here(self):
        with self.assertRaises(config.ConfigError):
            config.defaults_for({"dca": "console.dca_number"}, {})


class TestPrecedence(_TempConfig):
    """built-in default < config file < explicit flag, for every tool."""

    def resolve(self, module, argv, text=SAMPLE):
        path = self.write(text)
        args, used = config.resolve(module.parser(), module.CONFIG_MAPPING, argv, environ={}, search=[path])
        self.assertEqual(used, path)
        return args

    def test_serve_takes_every_mapped_value_from_the_config(self):
        args = self.resolve(serve, [])
        self.assertEqual(args.console_host, "10.0.0.5")
        self.assertEqual(args.console_port, 49900)
        self.assertEqual(args.dca, 3)
        self.assertIs(args.quantized, True)
        self.assertEqual(args.reaper_host, "127.0.0.1")
        self.assertEqual(args.reaper_port, 8000)
        self.assertEqual(args.reaper_feedback_port, 9000)
        self.assertEqual(args.fade, 2.5)
        self.assertEqual(args.listen, "0.0.0.0")
        self.assertEqual(args.http_port, 8080)
        self.assertEqual(args.log.name, "2026-09-13.jsonl")
        self.assertEqual(args.queue.name, "queue.tsv")

    def test_a_flag_beats_the_config(self):
        args = self.resolve(serve, ["--dca", "4", "--console-host", "10.0.0.9"])
        self.assertEqual(args.dca, 4)
        self.assertEqual(args.console_host, "10.0.0.9")

    def test_the_builtin_default_survives_a_config_that_is_silent(self):
        args = self.resolve(serve, [], text="[console]\nhost = '10.0.0.5'\ndca = 3\n")
        self.assertEqual(args.console_port, 49900)
        self.assertEqual(args.fade, 2.0)

    def test_a_config_boolean_can_still_be_refused_from_the_command_line(self):
        # store_true could not do this: the flag has no "off". Without it a
        # config saying quantized = true would be unanswerable on the day.
        args = self.resolve(serve, ["--no-quantized"])
        self.assertIs(args.quantized, False)

    def test_verify_dm7_shares_the_console_section(self):
        args = self.resolve(verify_dm7, ["--granularity"])
        self.assertEqual(args.host, "10.0.0.5")
        self.assertEqual(args.port, 49900)
        self.assertEqual(args.dca, 3)
        self.assertIs(args.quantized, True)

    def test_verify_reaper_shares_the_reaper_section(self):
        args = self.resolve(verify_reaper, [])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.send_port, 8000)
        self.assertEqual(args.recv_port, 9000)

    def test_the_config_flag_itself_is_read_before_the_file(self):
        path = self.write(SAMPLE, "elsewhere.toml")
        args, used = config.resolve(
            serve.parser(), serve.CONFIG_MAPPING, ["--config", str(path)], environ={}, search=[]
        )
        self.assertEqual(used, path)
        self.assertEqual(args.dca, 3)

    def test_no_config_leaves_every_builtin_default_alone(self):
        args, used = config.resolve(serve.parser(), serve.CONFIG_MAPPING, [], environ={}, search=[])
        self.assertIsNone(used)
        self.assertIsNone(args.console_host)
        self.assertEqual(args.console_port, 49900)
        self.assertEqual(args.fade, 2.0)


class TestFlagsTheConfigMustNotReach(_TempConfig):
    """Two flags read like config keys and are not. Both would misfire hardware."""

    def test_verify_dm7_fade_is_not_settable_from_the_config(self):
        # In verify_dm7, --fade is what *selects* the fade probe. Taking it from
        # fader.fade_seconds would fire a fade at the console on every run.
        path = self.write(SAMPLE)
        args, _ = config.resolve(verify_dm7.parser(), verify_dm7.CONFIG_MAPPING, [], environ={}, search=[path])
        self.assertIsNone(args.fade)
        self.assertNotIn("fade", verify_dm7.CONFIG_MAPPING)

    def test_verify_reaper_listen_is_a_duration_and_keeps_its_default(self):
        # ui.listen is a bind address; verify_reaper's --listen is seconds.
        path = self.write(SAMPLE)
        args, _ = config.resolve(verify_reaper.parser(), verify_reaper.CONFIG_MAPPING, [], environ={}, search=[path])
        self.assertEqual(args.listen, 20.0)
        self.assertNotIn("listen", verify_reaper.CONFIG_MAPPING)


class TestResolveOrExit(_TempConfig):
    """A broken config is a command-line error, not a traceback at the operator."""

    def test_a_misspelled_key_exits_with_the_message_and_no_traceback(self):
        path = self.write("[console]\ndca_number = 3\n")
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(stderr):
            config.resolve_or_exit(serve.parser(), serve.CONFIG_MAPPING, [], environ={}, search=[path])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("dca_number", stderr.getvalue())

    def test_broken_toml_exits_the_same_way(self):
        path = self.write("[console\n")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            config.resolve_or_exit(serve.parser(), serve.CONFIG_MAPPING, [], environ={}, search=[path])

    def test_a_named_config_that_is_absent_exits_the_same_way(self):
        stderr = io.StringIO()
        missing = str(self.dir / "absent.toml")
        with self.assertRaises(SystemExit), redirect_stderr(stderr):
            config.resolve_or_exit(serve.parser(), serve.CONFIG_MAPPING, ["--config", missing], environ={}, search=[])
        self.assertIn("absent.toml", stderr.getvalue())

    def test_a_good_config_still_comes_back_normally(self):
        path = self.write(SAMPLE)
        args, used = config.resolve_or_exit(serve.parser(), serve.CONFIG_MAPPING, [], environ={}, search=[path])
        self.assertEqual(used, path)
        self.assertEqual(args.dca, 3)


class TestRequire(_TempConfig):
    def test_a_missing_value_names_both_the_flag_and_the_config_key(self):
        p = serve.parser()
        args, _ = config.resolve(p, serve.CONFIG_MAPPING, [], environ={}, search=[])
        stderr = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(stderr):
            config.require(p, args, serve.CONFIG_MAPPING, *serve.REQUIRED)
        message = stderr.getvalue()
        self.assertIn("--console-host", message)
        self.assertIn("console.host", message)

    def test_a_value_supplied_by_the_config_satisfies_the_requirement(self):
        path = self.write(SAMPLE)
        p = serve.parser()
        args, _ = config.resolve(p, serve.CONFIG_MAPPING, [], environ={}, search=[path])
        config.require(p, args, serve.CONFIG_MAPPING, *serve.REQUIRED)  # must not raise


class TestTheExampleFile(unittest.TestCase):
    """The shipped example is the documentation, so it has to still be true."""

    EXAMPLE = Path(__file__).resolve().parent.parent / "tacet.toml.example"

    def test_it_exists(self):
        self.assertTrue(self.EXAMPLE.is_file(), f"{self.EXAMPLE} is missing")

    def test_it_validates_against_the_schema(self):
        # If a key is renamed in SCHEMA and not here, this fails rather than
        # leaving an example that errors the first time someone copies it.
        config.load(self.EXAMPLE)

    def test_it_shows_every_key(self):
        values = config.load(self.EXAMPLE)
        for option in config.SCHEMA:
            self.assertIn(option.name, values, f"{option.name} is not in the example file")

    def test_it_is_ascii(self):
        # Same trap as the source: a typographic character in a path or a level
        # is invisible here and broken when it is copy-pasted.
        self.EXAMPLE.read_text(encoding="utf-8").encode("ascii")


class TestSchemaAndMappingsAgree(unittest.TestCase):
    """Keeps the schema and the three mappings from drifting apart silently."""

    MAPPINGS = (serve.CONFIG_MAPPING, verify_dm7.CONFIG_MAPPING, verify_reaper.CONFIG_MAPPING)

    def test_every_mapping_points_at_a_key_in_the_schema(self):
        names = {option.name for option in config.SCHEMA}
        for mapping in self.MAPPINGS:
            for dest, name in mapping.items():
                self.assertIn(name, names, f"{dest} maps to {name}, which is not in the schema")

    def test_every_key_in_the_schema_is_used_by_at_least_one_tool(self):
        # A key nothing reads is a key that appears to work and does nothing.
        used = {name for mapping in self.MAPPINGS for name in mapping.values()}
        for option in config.SCHEMA:
            self.assertIn(option.name, used, f"{option.name} is in the schema but no tool reads it")

    def test_the_same_key_never_means_two_things(self):
        # console.host is --console-host in serve and --host in verify_dm7. That
        # is fine. What is not fine is one dest name meaning different keys in a
        # way the shared sections would collide on.
        seen: set[str] = set()
        for option in config.SCHEMA:
            self.assertNotIn(option.name, seen, f"{option.name} declared twice")
            seen.add(option.name)

    def test_every_option_has_help(self):
        for option in config.SCHEMA:
            self.assertTrue(option.help.strip(), f"{option.name} has no help")


if __name__ == "__main__":
    unittest.main()
