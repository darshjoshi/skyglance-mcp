"""Server-level contracts, including one that keeps the docs from lying.

The sibling project pitwall drifted to a skill claiming 67 tools while the server had 79,
and nothing caught it because nothing checked. This does.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from skyglance import server

REPO = Path(__file__).resolve().parent.parent


async def _tool_names() -> set[str]:
    return {t.name for t in await server.mcp.list_tools()}


class TestToolInventory:
    async def test_every_tool_has_a_description(self):
        """An undocumented tool is one the model will use wrongly."""
        for tool in await server.mcp.list_tools():
            assert tool.description, f"{tool.name} has no docstring"
            assert len(tool.description) > 40, f"{tool.name} description is too thin"

    @pytest.mark.parametrize("doc", [
        "README.md",
        ".claude/CLAUDE.md",
        "plugins/skyglance/README.md",
        "plugins/skyglance/skills/sky/SKILL.md",
    ])
    async def test_documented_tool_count_matches_reality(self, doc):
        """Every "N tools" claim in the docs must equal the real number."""
        actual = len(await _tool_names())
        text = (REPO / doc).read_text()
        claims = [int(n) for n in re.findall(r"(\d+)\s+(?:MCP\s+)?tools", text)]
        assert claims, f"{doc} makes no tool-count claim; expected at least one"
        for claimed in claims:
            assert claimed == actual, (
                f"{doc} claims {claimed} tools but the server registers {actual}")

    async def test_skill_routing_table_references_only_real_tools(self):
        """A skill pointing at a tool that doesn't exist sends the model nowhere.

        Scoped to the routing table specifically. The prose elsewhere legitimately names
        response *fields* in backticks (`arrival_estimate`, `coverage_note`), which are
        not tools, so scanning the whole document produces false positives.
        """
        names = await _tool_names()
        skill = (REPO / "plugins/skyglance/skills/sky/SKILL.md").read_text()
        rows = [line for line in skill.splitlines()
                if line.startswith("|") and line.count("|") >= 3]
        referenced = set()
        for row in rows:
            # The tool lives in the last column of a routing row.
            referenced.update(re.findall(r"`([a-z_]+)`", row.split("|")[-2]))
        assert referenced, "no routing rows found — has the skill's table moved?"
        unknown = referenced - names
        assert not unknown, f"routing table references non-existent tools: {sorted(unknown)}"

    async def test_every_tool_is_in_the_skill_routing_table(self):
        """A tool the skill never mentions is a tool the model won't reach for."""
        names = await _tool_names()
        skill = (REPO / "plugins/skyglance/skills/sky/SKILL.md").read_text()
        missing = {n for n in names if f"`{n}`" not in skill}
        assert not missing, f"tools absent from the skill routing table: {sorted(missing)}"


    def test_directory_skill_routes_exactly_the_directory_tools(self):
        """The spotter plugin's skill must not route to a tool that edition leaves out."""
        names = set(_tools_in_edition("directory"))
        skill = (REPO / "skills/sky/SKILL.md").read_text()
        missing = {n for n in names if f"`{n}`" not in skill}
        assert not missing, f"tools absent from the spotter skill: {sorted(missing)}"
        for left_out in TestDirectoryEdition.LEFT_OUT:
            assert left_out not in skill, f"spotter skill mentions {left_out}"
        claims = [int(n) for n in re.findall(r"(\d+)\s+(?:MCP\s+)?tools", skill)]
        assert claims and all(c == len(names) for c in claims), \
            f"spotter skill claims {claims} tools; the directory edition has {len(names)}"

class TestLocationResolution:
    def test_explicit_coordinates_win(self, monkeypatch):
        monkeypatch.setenv("SKYGLANCE_HOME_LAT", "51.47")
        monkeypatch.setenv("SKYGLANCE_HOME_LON", "-0.45")
        assert server.resolve_location(40.71, -74.01) == (40.71, -74.01)

    def test_falls_back_to_home(self, monkeypatch):
        monkeypatch.setenv("SKYGLANCE_HOME_LAT", "51.47")
        monkeypatch.setenv("SKYGLANCE_HOME_LON", "-0.45")
        assert server.resolve_location(None, None) == (51.47, -0.45)

    def test_no_location_raises_with_an_actionable_message(self, monkeypatch):
        monkeypatch.delenv("SKYGLANCE_HOME_LAT", raising=False)
        monkeypatch.delenv("SKYGLANCE_HOME_LON", raising=False)
        with pytest.raises(ValueError, match="Ask the user where they are"):
            server.resolve_location(None, None)

    @pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
    def test_out_of_range_coordinates_rejected(self, lat, lon):
        with pytest.raises(ValueError, match="out of range"):
            server.resolve_location(lat, lon)

    def test_malformed_home_env_is_ignored_not_fatal(self, monkeypatch):
        monkeypatch.setenv("SKYGLANCE_HOME_LAT", "not-a-number")
        monkeypatch.setenv("SKYGLANCE_HOME_LON", "-0.45")
        assert server.home() is None

    @pytest.mark.parametrize("lat", ["", "${user_config.home_lat}", "95", "nan"])
    def test_unset_or_invalid_plugin_setting_means_no_home(self, monkeypatch, lat):
        """The directory plugin passes its settings through as strings, possibly empty."""
        monkeypatch.setenv("SKYGLANCE_HOME_LAT", lat)
        monkeypatch.setenv("SKYGLANCE_HOME_LON", "-0.45")
        assert server.home() is None


class TestPollEnabled:
    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("True", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("", False), ("off", False),
    ])
    def test_explicit_values(self, monkeypatch, value, expected):
        monkeypatch.setenv("SKYGLANCE_POLL", value)
        assert server.poll_enabled() is expected

    def test_standalone_server_records_by_default(self, monkeypatch):
        monkeypatch.delenv("SKYGLANCE_POLL", raising=False)
        monkeypatch.setattr(server, "DIRECTORY_EDITION", False)
        assert server.poll_enabled() is True

    @pytest.mark.parametrize("value", [None, "${user_config.record_history}"])
    def test_directory_edition_records_only_when_opted_in(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("SKYGLANCE_POLL", raising=False)
        else:
            monkeypatch.setenv("SKYGLANCE_POLL", value)
        monkeypatch.setattr(server, "DIRECTORY_EDITION", True)
        assert server.poll_enabled() is False


def _tools_in_edition(edition: str) -> dict[str, dict]:
    """Tool name -> input schema, from a fresh interpreter (the edition is read at import)."""
    script = ("import asyncio, json; from skyglance import server; "
              "print(json.dumps({t.name: t.inputSchema "
              "for t in asyncio.run(server.mcp.list_tools())}))")
    env = {**os.environ, "SKYGLANCE_EDITION": edition}
    out = subprocess.run([sys.executable, "-c", script], env=env, check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out.strip().splitlines()[-1])


class TestDirectoryEdition:
    """The directory edition is the same server minus the aircraft-following features."""

    LEFT_OUT = {"military_aircraft", "privacy_blocked_aircraft"}

    def test_sensitive_tools_are_not_registered(self):
        tools = _tools_in_edition("directory")
        assert not self.LEFT_OUT & tools.keys()
        assert len(tools) == 22

    def test_search_has_no_military_filter(self):
        schema = _tools_in_edition("directory")["search_aircraft"]
        assert "military_only" not in schema["properties"]

    def test_standalone_server_keeps_everything(self):
        tools = _tools_in_edition("")
        assert self.LEFT_OUT <= tools.keys()
        assert "military_only" in tools["search_aircraft"]["properties"]
        assert len(tools) == 24

    def test_instructions_match_the_edition(self):
        """The instructions are edited by string replace; a no-op replace fails silently."""
        script = "from skyglance import server; print(repr(server.mcp.instructions))"
        env = {**os.environ, "SKYGLANCE_EDITION": "directory"}
        out = subprocess.run([sys.executable, "-c", script], env=env, check=True,
                             capture_output=True, text=True).stdout
        assert "military_aircraft" not in out
        assert "SKYGLANCE_HOME" not in out
        assert "plugin" in out

    async def test_every_tool_is_titled_and_read_only(self):
        for tool in await server.mcp.list_tools():
            assert tool.title, f"{tool.name} has no title"
            assert tool.annotations and tool.annotations.readOnlyHint, tool.name


class TestUnavailableIsNotZero:
    def test_unavailable_never_looks_like_an_empty_result(self):
        """The failure that produced "0 military aircraft" when 81 were airborne."""
        payload = server._unavailable("rate limited")
        assert payload["available"] is False
        assert "count" not in payload
        assert "NOT a result of zero" in payload["note"]


class TestPredictionHorizon:
    def test_horizon_is_within_the_measured_reliable_window(self):
        """Measured error: 0.11 km at 30s, 0.30 km at 60s, 1.15 km at 120s."""
        assert server.PREDICTION_HORIZON_S <= 120, (
            "beyond 120s a third of aircraft are >2 km from the prediction")


class TestVersionSync:
    """The release checklist says bump four files. A checklist is a hope; this is a test.

    A mismatch between pyproject and server.json is rejected outright by the MCP
    registry, and a stale plugin.json means plugin users never get offered the update.
    """

    @staticmethod
    def _pyproject_version() -> str:
        text = (REPO / "pyproject.toml").read_text()
        return re.search(r'^version = "([^"]+)"', text, re.M).group(1)

    def test_server_json_matches_pyproject(self):
        data = json.loads((REPO / "server.json").read_text())
        expected = self._pyproject_version()
        assert data["version"] == expected
        assert data["packages"][0]["version"] == expected, \
            "the registry rejects a package version that disagrees with PyPI"

    def test_plugin_manifest_matches_pyproject(self):
        data = json.loads(
            (REPO / "plugins/skyglance/.claude-plugin/plugin.json").read_text())
        assert data["version"] == self._pyproject_version()

    def test_marketplace_matches_pyproject(self):
        data = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
        assert data["plugins"][0]["version"] == self._pyproject_version()

    def test_plugin_launcher_pins_the_released_version(self):
        """uvx with no pin runs whatever PyPI serves; the directory blocks unpinned launchers."""
        data = json.loads((REPO / "plugins/skyglance/.mcp.json").read_text())
        args = data["mcpServers"]["skyglance"]["args"]
        assert f"skyglance=={self._pyproject_version()}" in args

    def test_spotter_marketplace_entry_matches_its_manifest(self):
        """The spotter plugin versions separately (it ships from source, not PyPI)."""
        market = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
        entry = next(p for p in market["plugins"] if p["name"] == "skyglance-spotter")
        manifest = json.loads((REPO / ".claude-plugin/plugin.json").read_text())
        assert entry["version"] == manifest["version"]

    def test_user_agent_reports_the_shipped_version(self):
        """Volunteer feed operators read this; a stale version makes their logs lie."""
        from skyglance.feeds import USER_AGENT
        major_minor = ".".join(self._pyproject_version().split(".")[:2])
        assert f"skyglance-mcp/{major_minor}" in USER_AGENT


class TestRegistryConstraints:
    """Limits the MCP registry enforces server-side but does NOT publish in its schema.

    Discovered the only way they can be: a release failed with HTTP 422 on
    `body.description: expected length <= 100`. The published JSON Schema declares no
    maxLength at all, so this is not derivable from the schema — it has to be pinned
    here or the next release finds it again at publish time, after PyPI has already
    taken the version number and made it unreusable.
    """

    MAX_DESCRIPTION = 100

    def test_description_fits_the_registry_limit(self):
        data = json.loads((REPO / "server.json").read_text())
        length = len(data["description"])
        assert length <= self.MAX_DESCRIPTION, (
            f"server.json description is {length} chars; the registry rejects anything "
            f"over {self.MAX_DESCRIPTION} with a 422 at publish time")

    def test_required_registry_fields_present(self):
        data = json.loads((REPO / "server.json").read_text())
        for field in ("$schema", "name", "description", "version", "packages"):
            assert data.get(field), f"server.json is missing {field!r}"
        assert data["name"].startswith("io.github."), \
            "the registry namespaces by GitHub owner"
        assert data["packages"][0]["registryType"] == "pypi"


class TestDirectorySubmissionRules:
    """Rules the Claude directory's validator enforced on this repo, kept as tests.

    Each one failed or warned in the developer portal once; see the commit that added it.
    """

    SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache"}

    def _repo_text_files(self):
        for path in REPO.rglob("*"):
            if path.is_file() and not self.SKIP_DIRS & set(path.relative_to(REPO).parts) \
                    and path.suffix in {".md", ".yml", ".yaml", ".json", ".py", ".toml", ".sh"}:
                yield path

    def test_no_package_manager_env_on_plugin_servers(self):
        """Blocking: any UV_*/PIP_*/npm env on a plugin's MCP server reads as a registry redirect."""
        for mcp_json in [REPO / ".mcp.json", REPO / "plugins/skyglance/.mcp.json"]:
            for name, server_cfg in json.loads(mcp_json.read_text())["mcpServers"].items():
                for key in server_cfg.get("env", {}):
                    assert not re.match(r"(UV|PIP|NPM|YARN|BUN|PNPM)_|npm_config_", key, re.I), \
                        f"{mcp_json.name}: {name} sets {key}"

    def test_nothing_pipes_a_download_into_a_program(self):
        """Warning: `curl ... | sh` (or | tar) is flagged as download-and-run, even in docs."""
        pattern = re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|tar|python)\b")
        offenders = [str(p.relative_to(REPO)) for p in self._repo_text_files()
                     if p.name != "test_server.py" and pattern.search(p.read_text(errors="ignore"))]
        assert not offenders, f"download piped into a program in: {offenders}"

    def test_contributor_notes_are_not_at_the_plugin_root(self):
        """Warning: CLAUDE.md at a plugin root isn't loaded; .claude/CLAUDE.md still is, locally."""
        assert not (REPO / "CLAUDE.md").exists()
        assert (REPO / ".claude/CLAUDE.md").exists()
