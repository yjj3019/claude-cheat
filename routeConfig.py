"""One settings file for every provider: ``openclaude.config.yaml``.

The file is a deliberately small YAML subset so the launcher and the dashboard can share one
stdlib-only reader and writer (the repo has no third-party dependencies)::

    # comments are kept
    openrouter:
      model: "vendor/model"
      api_key: "sk-or-..."
    local:
      url: "http://127.0.0.1:11434/v1/messages"

Supported: top-level ``section:`` lines, two-space ``key: value`` lines, ``#`` comments, plain,
"double" (``\\"`` and ``\\\\`` escapes only) and 'single' ('' escape) scalars. Everything else
(tabs, anchors, block scalars, lists, flow collections, duplicate keys, deeper nesting) is
rejected with a line number; values are never echoed in an error. All values are strings.

Callers keep using the historical environment-style names (``OPENROUTER_MODEL`` ...); the
mapping to YAML keys lives in ``SECTIONS``. A service that has no section yet falls back,
read-only, to its legacy dotenv file (``.env.openrouter`` / ``.env.local-llm``).
"""
import contextlib
import os
import random
import re
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_NAME = "openclaude.config.yaml"
EXAMPLE_NAME = "openclaude.config.example.yaml"
CONFIG_PATH = ROOT / CONFIG_NAME

SECTIONS = {
    "openrouter": {"legacy": ".env.openrouter",
                   "keys": {"model": "OPENROUTER_MODEL", "api_key": "ANTHROPIC_AUTH_TOKEN"}},
    "local": {"legacy": ".env.local-llm",
              "keys": {"url": "LOCAL_LLM_URL", "model": "LOCAL_LLM_MODEL", "api_key": "LOCAL_LLM_API_KEY",
                       "allow_remote": "LOCAL_LLM_ALLOW_REMOTE", "http_url": "LOCAL_LLM_HTTP_URL",
                       "models_url": "LOCAL_LLM_MODELS_URL"}},
    # Installed command-line clients (Codex, Grok, Claude Code). New in the unified file, so no legacy .env.
    "cli": {"legacy": None,
            "keys": {"kind": "CLI_KIND", "model": "CLI_MODEL", "binary": "CLI_BINARY", "timeout_s": "CLI_TIMEOUT_S"}},
    # Session routing behaviour. New in the unified file, so no legacy .env.
    "routing": {"legacy": None,
                "keys": {"dual_provider": "ROUTE_DUAL_PROVIDER", "default_provider": "ROUTE_DEFAULT_PROVIDER"}},
}

# Renaming over a file a Windows reader or scanner holds open fails with these.
_RETRY_WINERRORS = {5, 32, 33}  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
_RETRY_ATTEMPTS = 4
_WRITE_LOCK = threading.Lock()
# Characters that would break a line or are never meaningful in a setting; refused on write.
FORBIDDEN = re.compile("[\x00-\x1f\x7f\u0085  ]")
_BARE_BOOL = ("true", "false")


class ConfigError(ValueError):
    """A problem with the settings file. Carries a code and line number, never a value."""

    def __init__(self, code, line=None):
        self.code = code
        self.line = line
        super().__init__(code if line is None else f"{code} (line {line})")


def _split_lines(text):
    """Split on LF/CRLF only: str.splitlines() would also break on U+2028, U+0085 and friends."""
    return text.replace("\r\n", "\n").split("\n")


def _scalar(raw, line):
    """Decode the value part of ``key: value``; raises ConfigError on anything unsupported."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return ""
    first = raw[0]
    if first in "\"'":
        out, index = [], 1
        while index < len(raw):
            char = raw[index]
            if first == '"' and char == "\\":
                if index + 1 >= len(raw) or raw[index + 1] not in '"\\':
                    raise ConfigError("unsupported_escape", line)
                out.append(raw[index + 1])
                index += 2
                continue
            if char == first:
                if first == "'" and raw[index + 1:index + 2] == "'":
                    out.append("'")
                    index += 2
                    continue
                rest = raw[index + 1:].strip()
                if rest and not rest.startswith("#"):
                    raise ConfigError("trailing_text", line)
                return "".join(out)
            out.append(char)
            index += 1
        raise ConfigError("unterminated_string", line)
    if first in "&*!|>[{@`%]},," or raw.startswith(("- ", "? ")) or raw == "-":
        raise ConfigError("unsupported_syntax", line)
    comment = re.search(r"\s#", raw)
    value = (raw[:comment.start()] if comment else raw).rstrip()
    if ": " in value or value.endswith(":"):
        raise ConfigError("unsupported_syntax", line)
    return value


class Document:
    """Result of parse(): per-section values and errors, plus errors that affect every section."""

    def __init__(self):
        self.sections = {}      # name -> {"values": {yaml_key: str}, "errors": [ConfigError]}
        self.errors = []        # document-level syntax errors

    def first_error(self, section):
        if self.errors:
            return self.errors[0]
        entry = self.sections.get(section)
        return entry["errors"][0] if entry and entry["errors"] else None


def parse(text):
    """Parse the settings text. Never raises: problems are collected on the Document."""
    doc = Document()
    current = None  # None = no section yet, False = unknown section (body ignored), else a known name
    for number, raw in enumerate(_split_lines(text.lstrip("﻿")), start=1):
        body = raw.rstrip()
        stripped = body.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(body) - len(stripped)
        if indent == 0:
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):\s*(#.*)?", body)
            if not match:
                doc.errors.append(ConfigError("unsupported_syntax", number))
                current = None
                continue
            name = match.group(1)
            if name not in SECTIONS:
                current = False
                continue
            if name in doc.sections:
                doc.sections[name]["errors"].append(ConfigError("duplicate_section", number))
            else:
                doc.sections[name] = {"values": {}, "errors": []}
            current = name
            continue
        if current is False:
            continue  # body of a section this version does not know: kept and ignored
        if current is None:
            doc.errors.append(ConfigError("unsupported_syntax", number))
            continue
        errors = doc.sections[current]["errors"]
        # A tab is never counted as indentation, so a tab-indented line fails the entry pattern below.
        if indent != 2:
            errors.append(ConfigError("unsupported_nesting", number))
            continue
        entry = re.fullmatch(r"  ([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*))?", body)
        if not entry:
            errors.append(ConfigError("unsupported_syntax", number))
            continue
        key, rest = entry.group(1), entry.group(2) or ""
        if key not in SECTIONS[current]["keys"]:
            errors.append(ConfigError("unknown_key", number))
            continue
        if key in doc.sections[current]["values"]:
            errors.append(ConfigError("duplicate_key", number))
            continue
        try:
            value = _scalar(rest, number)
        except ConfigError as error:
            errors.append(error)
            continue
        if FORBIDDEN.search(value):
            errors.append(ConfigError("forbidden_character", number))
            continue
        doc.sections[current]["values"][key] = value
    return doc


def _dump(value):
    if not isinstance(value, str) or FORBIDDEN.search(value):
        raise ConfigError("invalid_value")
    if value in _BARE_BOOL:
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _trailing_comment(line):
    """The trailing ``# ...`` of a managed line, with its leading space, or "".

    Only a ``#`` outside quotes and preceded by whitespace starts a comment, so a value
    containing ``#`` (quoted or bare like ``v#1``) keeps it as part of the value.
    """
    quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                if quote == "'" and line[index + 1:index + 2] == "'":
                    index += 2
                    continue
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and index > 0 and line[index - 1] in " \t":
            return line[index - 1:]
        index += 1
    return ""


def update_text(text, section, updates):
    """Return ``text`` with ``updates`` ({yaml_key: str|None}) applied inside ``section``.

    Only that section's lines are touched; comments, other sections and unknown lines survive.
    ``None`` leaves a key as it is. Missing keys and sections are appended.
    """
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = _split_lines(text)
    if lines and lines[-1] == "":
        lines.pop()
    header = [i for i, line in enumerate(lines) if re.fullmatch(re.escape(section) + r":\s*(#.*)?", line.rstrip())]
    if len(header) > 1:
        raise ConfigError("duplicate_section", header[1] + 1)
    pending = {key: value for key, value in updates.items() if value is not None}
    if not header:
        if not pending:
            return eol.join(lines) + eol if lines else ""
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{section}:")
        lines.extend(f"  {key}: {_dump(value)}" for key, value in pending.items())
        return eol.join(lines) + eol
    start = header[0] + 1
    end = start
    while end < len(lines):
        probe = lines[end].rstrip()
        if probe and not probe.startswith("#") and not probe.startswith(" "):
            break
        end += 1
    body, written, last_entry = [], set(), start - 1
    for offset, line in enumerate(lines[start:end]):
        match = re.fullmatch(r"  ([A-Za-z_][A-Za-z0-9_]*):(?:\s.*)?", line.rstrip())
        if match and match.group(1) in updates:
            key = match.group(1)
            if key in written:
                continue  # collapse duplicates of a managed key
            written.add(key)
            value = updates[key]
            body.append(line if value is None else
                        f"  {key}: {_dump(value)}{_trailing_comment(line)}")
            last_entry = start + len(body) - 1
            continue
        body.append(line)
        if match:
            last_entry = start + len(body) - 1
    for key, value in pending.items():
        if key in written:
            continue
        insert_at = last_entry + 1 - start
        body.insert(insert_at, f"  {key}: {_dump(value)}")
        last_entry = start + insert_at
    return eol.join(lines[:start] + body + lines[end:]) + eol


def _replace_with_retry(source, target):
    """os.replace with bounded retries for Windows sharing/locking contention."""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            os.replace(source, target)
            return True
        except OSError as error:
            if getattr(error, "winerror", None) not in _RETRY_WINERRORS or attempt == _RETRY_ATTEMPTS - 1:
                return False
            time.sleep(0.05 * (2 ** attempt) + random.uniform(0, 0.01))
    return False


def read_legacy(path):
    """Strict single-line ``KEY=VALUE`` reader (the launcher's historical semantics)."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError("legacy_not_utf8") from None
    values = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not match:
            raise ConfigError("legacy_invalid_entry", number)
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


class Store:
    """The settings file at ``path``; legacy dotenv files and the example sit beside it."""

    def __init__(self, path=CONFIG_PATH):
        self.path = Path(path)

    def _legacy(self, provider):
        """The deprecated dotenv file for a service, or None when it never had one."""
        name = SECTIONS[provider]["legacy"]
        return self.path.with_name(name) if name else None

    def _example(self):
        return self.path.with_name(EXAMPLE_NAME)

    def _read_text(self):
        """File text, or None when the file does not exist. Undecodable files are an error."""
        return self._decode(self._read_bytes())

    def source(self, provider):
        """Where a service's settings come from: 'yaml', 'legacy' or 'none'."""
        text = self._read_text()
        if text is not None and provider in parse(text).sections:
            return "yaml"
        legacy = self._legacy(provider)
        return "legacy" if legacy is not None and legacy.is_file() else "none"

    def read_provider(self, provider):
        """Environment-style ``{NAME: value}`` for one service. Raises ConfigError/OSError."""
        spec = SECTIONS[provider]
        text = self._read_text()
        if text is not None:
            doc = parse(text)
            error = doc.first_error(provider)
            if error:
                raise error
            if provider in doc.sections:
                # An empty value means "not set" (the shipped example and a cleared field both use it),
                # so it never hides a built-in default such as the local Messages URL.
                return {spec["keys"][key]: value
                        for key, value in doc.sections[provider]["values"].items() if value != ""}
        legacy = self._legacy(provider)
        if legacy is None:
            return {}
        try:
            return read_legacy(legacy)
        except FileNotFoundError:
            return {}

    def write_provider(self, provider, updates, validate=None):
        """Apply updates atomically; optional validation sees the exact locked candidate before I/O."""
        spec = SECTIONS[provider]
        names = {name: key for key, name in spec["keys"].items()}
        yaml_updates = {}
        for name, value in updates.items():
            if name not in names:
                raise ConfigError("unknown_key")
            yaml_updates[names[name]] = value
        with _WRITE_LOCK, self._cross_process_lock():
            # Re-read inside the lock so a concurrent save is never overwritten with stale text.
            raw = self._read_bytes()
            original = self._decode(raw)
            if original is None:
                text = self._seed()
            else:
                doc = parse(original)
                if doc.errors:
                    raise doc.errors[0]
                text = original
                # A service whose section does not exist yet is still being served from its
                # deprecated .env file. Creating the section without those legacy values would
                # silently shadow (hide) the stored credential, so the section starts from them;
                # explicit posted values and None entries are left for update_text to decide.
                if provider not in doc.sections:
                    for key, value in self._legacy_values(provider).items():
                        if key not in yaml_updates or yaml_updates[key] is None:
                            yaml_updates[key] = value
            text = update_text(text, provider, yaml_updates)
            if validate is not None:
                candidate = parse(text)
                error = candidate.first_error(provider)
                if error:
                    raise error
                validate({spec["keys"][key]: value
                          for key, value in candidate.sections[provider]["values"].items() if value != ""})
            self._atomic_write(text, raw, original is not None and raw.startswith(b"\xef\xbb\xbf"))

    def reset_provider(self, provider, scope):
        """Clear a service's managed values from settings, its backup and legacy file.

        Each file is replaced atomically, but the three replacements are not a single
        transaction. A failure raises reset_incomplete; callers must not report success.
        This does not remove environment variables, external copies or live process memory.
        """
        if provider not in ("openrouter", "local") or scope not in ("credential", "service"):
            raise ConfigError("invalid_reset")
        keys = SECTIONS[provider]["keys"]
        selected = {"api_key"} if scope == "credential" else set(keys)
        updates = {key: "" for key in selected}
        legacy_names = {keys[key] for key in selected}

        def clean_yaml(raw, primary=False):
            text = self._decode(raw)
            document = parse(text)
            if document.errors or any(entry["errors"] for entry in document.sections.values()):
                raise ConfigError("reset_invalid_file")
            candidate_updates = dict(updates)
            if primary and scope == "credential" and provider not in document.sections:
                candidate_updates = {**self._legacy_values(provider), **candidate_updates}
            cleaned = update_text(text, provider, candidate_updates)
            # A credential may have been repeated in an inline comment on its own line.
            lines = cleaned.splitlines(keepends=True)
            in_section = False
            for index, line in enumerate(lines):
                body = line.rstrip("\r\n")
                if body and not body.startswith((" ", "#")):
                    in_section = bool(re.fullmatch(re.escape(provider) + r":\s*(#.*)?", body))
                if in_section and re.match(r"  api_key:\s", body) and "api_key" in selected:
                    ending = line[len(body):]
                    lines[index] = '  api_key: ""' + ending
            return "".join(lines).encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")

        def clean_legacy(path):
            # Validate before modifying; never echo paths or contents on error.
            read_legacy(path)
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
            lines = text.splitlines(keepends=True)
            lines = [line for line in lines if not (match := re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line))
                     or match.group(1) not in legacy_names]
            return "".join(lines).encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")

        def replace_without_backup(path, content):
            descriptor, temporary = tempfile.mkstemp(prefix=".reset.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                if not _replace_with_retry(temporary, path):
                    raise OSError("replace_failed")
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

        with _WRITE_LOCK, self._cross_process_lock():
            backup = self.path.with_name(self.path.name + ".bak")
            legacy = self._legacy(provider)
            paths = (self.path, backup, legacy)
            try:
                for path in paths:
                    if path is not None and (path.is_symlink() or (path.exists() and path.stat().st_nlink > 1)):
                        raise ConfigError("reset_unsafe_path")
                raw = self._read_bytes()
                # Seed retains other services' existing legacy settings if YAML is absent.
                primary = clean_yaml(raw if raw is not None else self._seed().encode("utf-8"), primary=True)
                backup_data = clean_yaml(backup.read_bytes()) if backup.is_file() else None
                legacy_data = clean_legacy(legacy) if legacy is not None and legacy.is_file() else None
            except ConfigError as error:
                if error.code == "reset_unsafe_path":
                    raise
                raise ConfigError("reset_invalid_file") from None
            except (OSError, UnicodeDecodeError):
                raise ConfigError("reset_invalid_file") from None
            try:
                # Never create a backup of the pre-reset content. Sanitise older copies first.
                if backup_data is not None:
                    replace_without_backup(backup, backup_data)
                if legacy_data is not None:
                    replace_without_backup(legacy, legacy_data)
                replace_without_backup(self.path, primary)
            except OSError:
                raise ConfigError("reset_incomplete") from None

    def default_provider(self):
        """An explicit preference for new sessions; old files retain automatic selection."""
        provider = self.read_provider("routing").get("ROUTE_DEFAULT_PROVIDER", "auto")
        if provider not in ("auto", "openrouter", "local", "cli", "sonnet"):
            raise ConfigError("invalid_default_provider")
        return provider

    def _legacy_values(self, provider):
        """The legacy dotenv values for one service, as {yaml_key: value} (missing file = {})."""
        spec = SECTIONS[provider]
        legacy = self._legacy(provider)
        if legacy is None or not legacy.is_file():
            return {}
        values = read_legacy(legacy)
        return {key: values[name] for key, name in spec["keys"].items() if name in values}

    def _read_bytes(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _decode(raw):
        if raw is None:
            return None
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ConfigError("config_not_utf8") from None

    @contextlib.contextmanager
    def _cross_process_lock(self, timeout=10.0):
        """Serialise saves across dashboard processes, not only across threads.

        Two dashboards on different ports (``--allow-port-fallback``) can save at the same
        time. An in-process lock alone lets both read the same text and replace it, which
        either loses one service's update or makes a replace fail with sharing contention.
        The lock lives in a sibling dot-file so it never looks like a settings file; git
        ignores it. A lock that cannot be opened fails closed: continuing without it could
        silently overwrite a concurrent dashboard's saved settings.
        """
        lock_path = self.path.with_name("." + self.path.name + ".lock")
        try:
            handle = open(lock_path, "a+b")
        except OSError:
            raise ConfigError("config_busy") from None
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise ConfigError("config_busy") from None
                        time.sleep(0.02)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                acquired = True
            yield
        finally:
            if acquired:
                try:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    def _seed(self):
        """Text for a first save: the shipped example, plus every service's legacy values."""
        try:
            base = self._example().read_text(encoding="utf-8-sig")
        except (FileNotFoundError, UnicodeDecodeError):
            base = ""
        for provider, spec in SECTIONS.items():
            if self._legacy(provider) is None:
                continue
            try:
                legacy = read_legacy(self._legacy(provider))
            except FileNotFoundError:
                continue
            yaml_values = {key: legacy[name] for key, name in spec["keys"].items() if name in legacy}
            if yaml_values:
                base = update_text(base, provider, yaml_values)
        return base

    def _atomic_write(self, text, original_bytes, bom=False):
        descriptor, temporary = tempfile.mkstemp(prefix="." + CONFIG_NAME + ".", suffix=".tmp",
                                                 dir=str(self.path.parent))
        try:
            # newline="" keeps the line endings update_text detected from the original text;
            # utf-8-sig only when the original file carried a byte-order mark.
            with os.fdopen(descriptor, "w", encoding="utf-8-sig" if bom else "utf-8",
                           newline="") as handle:
                handle.write(text)
            if original_bytes is not None:
                backup = self.path.with_name(self.path.name + ".bak")
                # Open with restrictive permissions before writing credentials. Reject
                # symlinks where supported instead of following a pre-existing backup link.
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(backup, flags, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    if os.name != "nt":
                        os.fchmod(handle.fileno(), 0o600)
                    handle.write(original_bytes)
            if not _replace_with_retry(temporary, self.path):
                raise OSError("could not replace the settings file")
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass


DEFAULT = Store()


def read_provider(provider):
    return DEFAULT.read_provider(provider)
