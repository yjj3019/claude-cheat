"""Version identity, update comparison and the badge wiring.

The update check is the only place this product talks to a git remote, so the tests pin
the contract: read-only commands, cached answers, honest failure states, and a request
path that never waits for git or the network.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# The dashboard and the launcher both `import routeVersion`; the tests must patch that
# same instance, so it is imported normally rather than loaded a second time.
sys.path.insert(0, str(ROOT))
import routeVersion as version  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dash = load("routeDashboard.py")

HEAD = "a" * 40
REMOTE = "b" * 40


def porcelain(commit=HEAD, branch="main", dirty=False, upstream="+0 -0"):
    lines = [f"# branch.oid {commit}", f"# branch.head {branch}"]
    if upstream:
        lines.append(f"# branch.ab {upstream}")
    if dirty:
        lines.append("1 .M N... 100644 100644 100644 " + "c" * 40 + " " + "c" * 40 + " routeVersion.py")
    return "\n".join(lines)


class FakeGit:
    """A read-only git stand-in that also records which commands were issued."""

    def __init__(self, status=None, status_error=None, commit_date="2026-01-02T03:04:05+09:00",
                 remote_sha=None, remote_error=None, remote_branch="main",
                 remote_commit_known=True, behind=0, ahead=0):
        self.status = status if status is not None else porcelain()
        self.status_error = status_error
        self.commit_date = commit_date
        self.remote_sha = remote_sha
        self.remote_error = remote_error
        self.remote_branch = remote_branch
        self.remote_commit_known = remote_commit_known
        self.behind = behind
        self.ahead = ahead
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        head = args[0]
        if head == "status":
            if self.status_error:
                return 128, "", self.status_error
            return 0, self.status, ""
        if head == "log":
            if not self.commit_date:
                return 1, "", "no commits"
            return 0, self.commit_date, ""
        if head == "ls-remote":
            if self.remote_error:
                return 128, "", self.remote_error
            if not self.remote_sha:
                return 0, "", ""
            return 0, f"{self.remote_sha}\trefs/heads/{self.remote_branch}", ""
        if head == "cat-file":
            return (0, "", "") if self.remote_commit_known else (128, "", "not found")
        if head == "rev-list":
            span = args[-1]
            # "<base>..HEAD" counts commits the remote lacks; "HEAD..<remote>" counts ours.
            return 0, str(self.ahead if span.endswith(".." + HEAD) else self.behind), ""
        return 127, "", f"unexpected git {head}"


class VersionModuleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(version, "VERSION_FILE", self.root / "VERSION"))
        self.enterContext(mock.patch.object(version, "UPDATE_CACHE_FILE", self.root / ".update-cache.json"))
        self.enterContext(mock.patch.dict(version._LOCAL,
                                          {"at": 0.0, "signature": None, "data": None, "inflight": False},
                                          clear=True))
        self.enterContext(mock.patch.dict(version._REMOTE,
                                          {"at": 0.0, "data": None, "inflight": False}, clear=True))
        for name in ("ROUTE_UPDATE_CHECK", "ROUTE_UPDATE_REMOTE", "ROUTE_UPDATE_BRANCH",
                     "ROUTE_UPDATE_TIMEOUT_MS", "ROUTE_UPDATE_TTL_SECONDS"):
            self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
            os.environ.pop(name, None)
        self.write_version("1.0.0")

    def write_version(self, text):
        (self.root / "VERSION").write_text(text, encoding="utf-8")

    def test_git_runs_without_a_console_window(self):
        completed = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        with mock.patch.object(version.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(version.subprocess, "run", return_value=completed) as run:
            self.assertEqual(version._git(["status"]), (0, "ok", ""))
        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    # -- declared version ------------------------------------------------------------
    def test_declared_version_reads_the_file_and_normalises_a_v_prefix(self):
        self.assertEqual(version.declared_version(), "1.0.0")
        self.write_version("v2.3.4")
        self.assertEqual(version.declared_version(), "2.3.4")
        self.write_version("1.2.0-rc.1\n")
        self.assertEqual(version.declared_version(), "1.2.0-rc.1")

    def test_declared_version_falls_back_when_the_file_is_missing_or_wrong(self):
        self.write_version("not a version")
        self.assertEqual(version.declared_version(), version.FALLBACK_VERSION)
        (self.root / "VERSION").unlink()
        self.assertEqual(version.declared_version(), version.FALLBACK_VERSION)

    # -- local checkout facts --------------------------------------------------------
    def test_local_read_parses_commit_branch_and_dirtiness_in_two_git_calls(self):
        git = FakeGit(status=porcelain(dirty=True))
        with mock.patch.object(version, "_git", git):
            data = version._read_local()
        self.assertEqual(data["commit"], HEAD)
        self.assertEqual(data["commit_short"], HEAD[:7])
        self.assertEqual(data["branch"], "main")
        self.assertIs(data["dirty"], True)
        self.assertEqual(data["commit_date"], "2026-01-02T03:04:05+09:00")
        self.assertEqual(data["label"], f"v1.0.0 ({HEAD[:7]}*)")
        self.assertEqual([call[0] for call in git.calls], ["status", "log"])

    def test_clean_and_detached_checkouts_are_reported_without_invention(self):
        with mock.patch.object(version, "_git", FakeGit(status=porcelain(dirty=False, branch="(detached)"))):
            data = version._read_local()
        self.assertIs(data["dirty"], False)
        self.assertIsNone(data["branch"])
        self.assertEqual(data["label"], f"v1.0.0 ({HEAD[:7]})")

    def test_an_unreadable_checkout_keeps_the_declared_version_and_reports_the_reason(self):
        with mock.patch.object(version, "_git", FakeGit(status_error="fatal: not a git repository")):
            data = version._read_local()
        self.assertEqual(data["version"], "1.0.0")
        self.assertEqual(data["source"], "unavailable")
        self.assertIn("not a git repository", data["error"])
        self.assertIsNone(data["commit"])

    def test_local_info_is_cached_until_explicit_refresh(self):
        git = FakeGit()
        with mock.patch.object(version, "_git", git), \
                mock.patch.object(version, "_local_signature", return_value=(1, 2)):
            first = version.local_info(blocking=True)
            calls_after_first = len(git.calls)
            second = version.local_info(blocking=True)
        self.assertEqual(calls_after_first, 2)
        self.assertEqual(len(git.calls), calls_after_first, "a cached read must not spawn git again")
        self.assertTrue(second["cached"])
        self.assertEqual(first["commit"], second["commit"])
        with mock.patch.object(version, "_git", git), \
                mock.patch.object(version, "_local_signature", return_value=(9, 9)):
            version.local_info(blocking=True)
            self.assertEqual(len(git.calls), calls_after_first)
            version.local_info(refresh=True, blocking=True)
        self.assertGreater(len(git.calls), calls_after_first)

    def test_non_blocking_read_returns_a_placeholder_then_fills_in(self):
        started = threading.Event()

        def slow_read():
            started.set()
            time.sleep(0.4)
            return {"version": "1.0.0", "label": "v1.0.0 (abcdef0)", "commit": HEAD,
                    "commit_short": HEAD[:7], "branch": "main", "dirty": False,
                    "commit_date": None, "tag": None, "upstream": None,
                    "declared_version": "1.0.0", "source": "git", "error": None}

        with mock.patch.object(version, "_read_local", slow_read), \
                mock.patch.object(version, "_local_signature", return_value=(1, 2)):
            clock = time.perf_counter()
            first = version.local_info()
            elapsed = time.perf_counter() - clock
            self.assertTrue(started.wait(2), "the background read never started")
            deadline = time.time() + 3
            while time.time() < deadline and version._LOCAL["data"] is None:
                time.sleep(0.02)
            second = version.local_info()
        self.assertLess(elapsed, 0.2, "the render path must not wait for git")
        self.assertTrue(first.get("pending"))
        self.assertEqual(first["label"], "v1.0.0")
        self.assertEqual(second["label"], "v1.0.0 (abcdef0)")

    # -- comparison ------------------------------------------------------------------
    def test_comparison_names_current_behind_ahead_diverged_and_unknown(self):
        cases = [(HEAD, True, 0, 0, "current"), (REMOTE, True, 3, 0, "behind"),
                 (REMOTE, True, 0, 2, "ahead"), (REMOTE, True, 1, 4, "diverged")]
        for remote_sha, known, behind, ahead, expected in cases:
            with self.subTest(expected=expected):
                git = FakeGit(remote_commit_known=known, behind=behind, ahead=ahead)
                with mock.patch.object(version, "_git", git):
                    result = version._compare(HEAD, remote_sha)
                self.assertEqual(result["state"], expected)
        with mock.patch.object(version, "_git", FakeGit(remote_commit_known=False)):
            self.assertEqual(version._compare(HEAD, REMOTE)["state"], "different")

    def test_comparison_ignores_a_commit_that_was_never_fetched(self):
        # A remote-only commit cannot be counted; the badge must not invent a number.
        with mock.patch.object(version, "_git", FakeGit(remote_commit_known=False)):
            result = version._compare(HEAD, REMOTE)
        self.assertIsNone(result["behind_by"])
        self.assertIsNone(result["ahead_by"])

    # -- update check ----------------------------------------------------------------
    def test_update_check_is_disableable_and_touch_no_git(self):
        git = FakeGit()
        with mock.patch.dict(os.environ, {"ROUTE_UPDATE_CHECK": "off"}), \
                mock.patch.object(version, "_git", git):
            report = version.version_report(async_update=False, blocking=True)
        self.assertEqual(report["update"]["state"], "disabled")
        self.assertEqual([call[0] for call in git.calls], ["status", "log"],
                         "a disabled check must still read the local version only")

    def test_update_reports_current_when_the_remote_matches(self):
        git = FakeGit(remote_sha=HEAD)
        with mock.patch.object(version, "_git", git):
            report = version.version_report(refresh=True, async_update=False, blocking=True)
        update = report["update"]
        self.assertEqual(update["state"], "current")
        self.assertEqual(update["remote_commit"], HEAD)
        self.assertEqual(update["remote_branch"], "main")
        self.assertIsNotNone(update["checked_at"])
        self.assertIn("ls-remote", [call[0] for call in git.calls])

    def test_update_counts_the_commits_a_pull_would_bring(self):
        git = FakeGit(remote_sha=REMOTE, behind=3, ahead=0)
        with mock.patch.object(version, "_git", git):
            report = version.version_report(refresh=True, async_update=False, blocking=True)
        self.assertEqual(report["update"]["state"], "behind")
        self.assertEqual(report["update"]["behind_by"], 3)
        self.assertIn("업데이트 있음", version.notice(report))
        self.assertIn("3개 커밋", version.summarize(report) + version.notice(report))

    def test_an_offline_machine_degrades_to_unknown_with_the_reason(self):
        git = FakeGit(remote_error="fatal: unable to access 'https://github.com/...': Could not resolve host")
        with mock.patch.object(version, "_git", git):
            report = version.version_report(refresh=True, async_update=False, blocking=True)
        self.assertEqual(report["update"]["state"], "unknown")
        self.assertIn("Could not resolve host", report["update"]["error"])
        self.assertIn("확인 불가", version.notice(report))

    def test_a_missing_remote_branch_is_reported_not_guessed(self):
        git = FakeGit(remote_sha=None)
        with mock.patch.object(version, "_git", git):
            report = version.version_report(refresh=True, async_update=False, blocking=True)
        self.assertEqual(report["update"]["state"], "unknown")
        self.assertIn("no branch", report["update"]["error"])

    def test_a_detached_head_asks_for_an_explicit_branch(self):
        git = FakeGit(status=porcelain(branch="(detached)"), remote_sha=HEAD)
        with mock.patch.object(version, "_git", git):
            report = version.version_report(refresh=True, async_update=False, blocking=True)
        self.assertEqual(report["update"]["state"], "unknown")
        self.assertIn("ROUTE_UPDATE_BRANCH", report["update"]["error"])

    def test_the_remote_answer_is_reused_inside_the_ttl(self):
        git = FakeGit(remote_sha=HEAD)
        local = {"commit": HEAD, "branch": "main"}
        with mock.patch.object(version, "_git", git):
            first = version.update_status(refresh=True, async_refresh=False, local=local)
            calls = len(git.calls)
            second = version.update_status(refresh=False, async_refresh=False, local=local)
        self.assertEqual(len(git.calls), calls, "a cached answer must not re-run ls-remote")
        self.assertTrue(second["cached"])
        self.assertEqual(first["state"], second["state"])

    # -- persisted cache -------------------------------------------------------------
    def test_the_last_answer_is_persisted_and_seeds_a_fresh_launcher(self):
        version._save_update_cache({"state": "behind", "checked_at": "2026-01-02T03:04:05+00:00",
                                    "remote": "origin", "remote_branch": "main",
                                    "remote_commit": REMOTE, "behind_by": 2, "ahead_by": 0,
                                    "error": None, "local_commit": HEAD})
        self.assertTrue((self.root / ".update-cache.json").is_file())
        version._REMOTE.update({"at": 0.0, "data": None})
        version._seed_update_cache()
        self.assertEqual(version._REMOTE["data"]["state"], "behind")
        self.assertEqual(version._seed_update_cache(), None)

    def test_a_corrupt_or_foreign_cache_is_ignored(self):
        (self.root / ".update-cache.json").write_text("{oops", encoding="utf-8")
        self.assertIsNone(version._load_update_cache())
        (self.root / ".update-cache.json").write_text(
            json.dumps({"version": 999, "data": {"state": "behind"}}), encoding="utf-8")
        self.assertIsNone(version._load_update_cache())

    def test_a_launcher_banner_uses_the_persisted_answer_without_git_or_network(self):
        version._save_update_cache({"state": "behind", "checked_at": None, "remote": "origin",
                                    "remote_branch": "main", "remote_commit": REMOTE,
                                    "behind_by": 2, "ahead_by": 0, "error": None, "local_commit": HEAD})
        git = FakeGit(remote_sha=HEAD)
        with mock.patch.object(version, "_git", git), \
                mock.patch.object(version, "_local_signature", return_value=(1, 2)):
            clock = time.perf_counter()
            report = version.version_report()
            elapsed = time.perf_counter() - clock
        self.assertLess(elapsed, 0.2)
        self.assertTrue(report["update"]["previous_check"])
        self.assertIn("업데이트 있음", version.notice(report))
        self.assertIn("마지막 확인 기준", version.notice(report))

    # -- presentation ----------------------------------------------------------------
    def test_notice_and_summarize_name_the_build_branch_and_update_state(self):
        report = {"local": {"label": "v1.0.0 (abcdef0)", "version": "1.0.0", "branch": "main",
                            "commit": HEAD, "commit_date": "2026-01-02T03:04:05+09:00",
                            "dirty": False},
                  "update": {"state": "ahead", "ahead_by": 2, "behind_by": 0, "checked_at": None,
                             "remote_commit": REMOTE, "error": None}}
        line = version.notice(report)
        self.assertIn("v1.0.0 (abcdef0)", line)
        self.assertIn("main", line)
        self.assertIn("앞섬", line)
        text = version.summarize(report)
        self.assertIn("OpenClaude v1.0.0 (abcdef0)", text)
        self.assertIn("브랜치: main", text)

    def test_a_report_without_an_update_section_says_so(self):
        report = {"local": {"label": "v1.0.0"}}
        self.assertIn("확인 안 함", version.notice(report))
        self.assertIn("확인 안 함", version.summarize(report))


class DashboardVersionTests(unittest.TestCase):
    """The badge has to sit on the page and answer fast, whatever git is doing."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(version, "UPDATE_CACHE_FILE", self.root / ".update-cache.json"))
        self.enterContext(mock.patch.dict(version._LOCAL,
                                          {"at": 0.0, "signature": None, "data": None, "inflight": False},
                                          clear=True))
        self.enterContext(mock.patch.dict(version._REMOTE,
                                          {"at": 0.0, "data": None, "inflight": False}, clear=True))

    def serve(self):
        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def get(self, url, timeout=10):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)

    def test_every_page_carries_the_version_control(self):
        for page in ("dashboard", "settings", "usage", "blackbox"):
            with self.subTest(page=page):
                html = dash._page(page)
                self.assertIn('id="version-pill"', html)
                self.assertIn("checkVersionNow", html)
                self.assertIn("loadVersion", html)

    def test_api_version_answers_without_waiting_for_git(self):
        declared_version = version.declared_version()
        def slow_read():
            time.sleep(1.5)
            return {"version": "1.0.0", "label": "v1.0.0 (abcdef0)", "commit": HEAD,
                    "commit_short": HEAD[:7], "branch": "main", "dirty": False,
                    "commit_date": None, "tag": None, "upstream": None,
                    "declared_version": "1.0.0", "source": "git", "error": None}

        base = self.serve()
        with mock.patch.object(version, "_read_local", slow_read), \
                mock.patch.object(version, "_local_signature", return_value=(1, 2)):
            clock = time.perf_counter()
            status, body, headers = self.get(base + "/api/version")
            elapsed = time.perf_counter() - clock
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 0.8, "the endpoint blocked on the checkout read")
        payload = json.loads(body)
        self.assertEqual(payload["local"]["version"], declared_version)
        self.assertEqual(payload["update"]["state"], "disabled")
        self.assertIn("application/json", headers["content-type"])

    def test_api_version_does_not_check_remote(self):
        git = FakeGit(remote_sha=REMOTE, behind=2)
        base = self.serve()
        with mock.patch.object(version, "_git", git), \
                mock.patch.object(version, "_local_signature", return_value=(1, 2)):
            self.get(base + "/api/version")
            deadline = time.time() + 5
            payload = {}
            while time.time() < deadline:
                _, body, _ = self.get(base + "/api/version")
                payload = json.loads(body)
                if payload["local"].get("commit") == HEAD:
                    break
                time.sleep(0.05)
        self.assertEqual(payload["local"]["commit"], HEAD)
        self.assertEqual(payload["update"]["state"], "disabled")
        self.assertFalse(any(call[0] == "ls-remote" for call in git.calls))

    def test_the_snapshot_carries_the_version_block(self):
        with mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()), \
                mock.patch.object(dash, "parse_route_log", return_value=[]), \
                mock.patch.object(dash, "account_state", return_value={"ok": False, "error": "configuration_missing"}), \
                mock.patch.object(dash, "model_pricing", return_value={"ok": False, "error": "configuration_missing"}), \
                mock.patch.object(dash, "proactive_alerts", return_value=[]), \
                mock.patch.object(dash, "read_settings", return_value={}):
            data = dash.snapshot()
        self.assertIn("local", data["version"])
        self.assertEqual(data["version"]["update"]["state"], "disabled")

    def test_status_report_reads_the_version_blocking_only_when_asked(self):
        with mock.patch.object(dash.routeVersion, "local_info", return_value={"label": "v1.0.0"}) as probe:
            dash.status_report(offline=True)
            probe.assert_called_with(blocking=False)
            dash.status_report(offline=True, block_version=True)
            probe.assert_called_with(blocking=True)


class VersionCliTests(unittest.TestCase):
    def test_the_dashboard_cli_prints_a_summary_and_exits_zero(self):
        report = {"local": {"label": "v1.0.0 (abcdef0)", "branch": "main", "commit": HEAD,
                            "commit_date": None, "dirty": False, "version": "1.0.0"},
                  "update": {"state": "behind", "behind_by": 1, "ahead_by": 0,
                             "checked_at": None, "remote_commit": REMOTE, "error": None},
                  "generated_at": "2026-01-02T03:04:05+00:00"}
        with mock.patch.object(dash.routeVersion, "version_report", return_value=report), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = dash.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("OpenClaude v1.0.0 (abcdef0)", output.getvalue())
        self.assertIn("업데이트", output.getvalue())

    def test_the_dashboard_cli_prints_json_on_request(self):
        report = {"local": {"label": "v1.0.0"}, "update": {"state": "current"}}
        with mock.patch.object(dash.routeVersion, "version_report", return_value=report), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = dash.main(["--version", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_the_launcher_reports_the_version_without_starting_a_session(self):
        advisor = load("openrouterAdvisor.py")
        report = {"local": {"label": "v1.0.0 (abcdef0)", "branch": "main", "commit": HEAD,
                            "commit_date": None, "dirty": False, "version": "1.0.0"},
                  "update": {"state": "current", "behind_by": 0, "ahead_by": 0,
                             "checked_at": None, "remote_commit": HEAD, "error": None}}
        with mock.patch.object(advisor.routeVersion, "version_report", return_value=report):
            self.assertEqual(advisor.version_banner(), "OpenClaude v1.0.0 (abcdef0)")
        with mock.patch.object(advisor.routeVersion, "version_report",
                               return_value={**report, "update": {**report["update"], "state": "behind",
                                                                  "behind_by": 2}}):
            banner = advisor.version_banner()
        self.assertIn("업데이트 있음", banner)
        self.assertIn("git pull", banner)

    def test_the_launcher_banner_survives_a_broken_version_probe(self):
        advisor = load("openrouterAdvisor.py")
        with mock.patch.object(advisor.routeVersion, "version_report", side_effect=RuntimeError("git missing")):
            self.assertEqual(advisor.version_banner(), "")


if __name__ == "__main__":
    unittest.main()
