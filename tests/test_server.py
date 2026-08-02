"""Server-level contracts, including one that keeps the docs from lying.

The sibling project pitwall drifted to a skill claiming 67 tools while the server had 79,
and nothing caught it because nothing checked. This does.
"""

import json
import re
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
        "CLAUDE.md",
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

    def test_user_agent_reports_the_shipped_version(self):
        """Volunteer feed operators read this; a stale version makes their logs lie."""
        from skyglance.feeds import USER_AGENT
        major_minor = ".".join(self._pyproject_version().split(".")[:2])
        assert f"skyglance-mcp/{major_minor}" in USER_AGENT
