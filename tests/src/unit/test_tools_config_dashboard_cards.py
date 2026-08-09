"""Unit tests for the card/view dashboard tools in tools_config_dashboards.

Covers ``ha_config_set_card``, ``ha_config_remove_card``,
``ha_config_list_view_sections``, ``ha_config_set_view``, and
``ha_config_remove_view``: the mutation semantics (insert/replace/move,
section targeting, view add/merge/remove), the dry_run zero-write contract
(no ``lovelace/config/save`` round-trip), and the storage-mode guard.

The WS client is stubbed with a router keyed on the message ``type`` so the
same fake serves every call site regardless of call order; the shared config
dict is mutated in place by the tools (exactly as the real save path would
serialize it), so assertions on the saved config read the router's dict after
the call returns.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

# -----------------------------------------------------------------------------
# Fixtures / helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return client


def _config_with_flat_view() -> dict:
    """A config with one flat (masonry-style) view carrying three cards."""
    return {
        "views": [
            {
                "title": "Home",
                "path": "home",
                "cards": [
                    {"type": "markdown", "content": "one"},
                    {"type": "button", "entity": "light.bedroom"},
                    {"type": "tile", "entity": "climate.living_room"},
                ],
            }
        ]
    }


def _config_with_sections_view() -> dict:
    """A config with one 'sections'-type view carrying a single section."""
    return {
        "views": [
            {
                "title": "Sections",
                "type": "sections",
                "sections": [
                    {
                        "title": "Climate",
                        "heading": "Climate zone",
                        "cards": [{"type": "tile", "entity": "climate.living_room"}],
                    }
                ],
            }
        ]
    }


def _stub_ws(
    client: MagicMock,
    config: dict,
    dashboards: list[dict] | None = None,
) -> None:
    """Route WS replies by message ``type``.

    ``config`` is the same object the tools mutate in place; ``dashboards``
    defaults to one storage-mode ``my-dash`` row so the storage guard passes
    for the standard test dashboard.
    """
    if dashboards is None:
        dashboards = [{"id": "my_dash", "url_path": "my-dash", "mode": "storage"}]

    async def router(data: dict) -> dict:
        msg_type = data.get("type")
        if msg_type == "lovelace/config":
            return {"success": True, "result": config}
        if msg_type == "lovelace/dashboards/list":
            return {"success": True, "result": dashboards}
        if msg_type == "lovelace/config/save":
            return {"success": True}
        raise AssertionError(f"Unexpected WS message type: {msg_type}")

    client.send_websocket_message.side_effect = router


def _ws_types(client: MagicMock) -> list[str]:
    """The ``type`` of every WS message the tool sent, in order."""
    return [
        call.args[0].get("type")
        for call in client.send_websocket_message.call_args_list
    ]


def _error_message(exc_info: pytest.ExceptionInfo) -> str:
    """The structured error message inside a raised ``ToolError``."""
    body = json.loads(str(exc_info.value))
    return body["error"]["message"]


# -----------------------------------------------------------------------------
# ha_config_set_card — insert / replace / move
# -----------------------------------------------------------------------------


class TestSetCard:
    @pytest.fixture
    def set_card(self, fake_client):
        return DashboardConfigTools(fake_client).ha_config_set_card

    @pytest.fixture
    def new_card(self) -> dict:
        return {"type": "markdown", "content": "new card"}

    async def test_insert_appends_by_default(self, fake_client, set_card, new_card):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_card(url_path="my-dash", view="0", card=new_card)

        assert result == {
            "success": True,
            "url_path": "my-dash",
            "summary": "Inserted new card at index 3",
        }
        # The saved config (router's in-place-mutated dict) carries the card.
        assert config["views"][0]["cards"][3] == new_card
        assert _ws_types(fake_client) == [
            "lovelace/config",
            "lovelace/dashboards/list",
            "lovelace/config/save",
        ]

    async def test_insert_at_position(self, fake_client, set_card, new_card):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_card(url_path="my-dash", view="0", card=new_card, position=0)

        assert result["summary"] == "Inserted new card at index 0"
        assert config["views"][0]["cards"][0] == new_card
        assert config["views"][0]["cards"][1]["content"] == "one"

    async def test_insert_dry_run_previews_without_save(
        self, fake_client, set_card, new_card
    ):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_card(
            url_path="my-dash", view="0", card=new_card, dry_run=True
        )

        assert result["dry_run"] is True
        assert result["url_path"] == "my-dash"
        assert result["summary"] == "Inserted new card at index 3"
        # Preview carries the mutated config, but nothing is saved.
        assert result["config"]["views"][0]["cards"][3] == new_card
        assert "lovelace/config/save" not in _ws_types(fake_client)
        assert fake_client.send_websocket_message.call_count == 2

    async def test_insert_into_sections_view(self, fake_client, set_card, new_card):
        config = _config_with_sections_view()
        _stub_ws(fake_client, config)

        result = await set_card(url_path="my-dash", view="0", section=0, card=new_card)

        assert result["summary"] == "Inserted new card at index 1"
        assert config["views"][0]["sections"][0]["cards"][1] == new_card
        assert "lovelace/config/save" in _ws_types(fake_client)

    async def test_insert_without_card_raises(self, fake_client, set_card):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await set_card(url_path="my-dash", view="0")

        assert "card is required for insert" in _error_message(exc_info)
        assert "lovelace/config/save" not in _ws_types(fake_client)

    async def test_replace_at_index(self, fake_client, set_card):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)
        replacement = {"type": "markdown", "content": "replaced"}

        result = await set_card(
            url_path="my-dash", view="0", card_index=1, card=replacement
        )

        assert result["summary"] == "Replaced card at index 1"
        assert config["views"][0]["cards"][1] == replacement
        assert len(config["views"][0]["cards"]) == 3

    async def test_replace_out_of_range_raises(self, fake_client, set_card, new_card):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await set_card(url_path="my-dash", view="0", card_index=9, card=new_card)

        assert "Card index 9 out of range" in _error_message(exc_info)
        assert "lovelace/config/save" not in _ws_types(fake_client)

    async def test_replace_without_card_raises(self, fake_client, set_card):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await set_card(url_path="my-dash", view="0", card_index=1)

        assert "card is required for replace" in _error_message(exc_info)

    async def test_move_reorders(self, fake_client, set_card):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_card(url_path="my-dash", view="0", card_index=0, position=2)

        assert result["summary"] == "Moved card from index 0 to 2"
        assert len(config["views"][0]["cards"]) == 3
        # The markdown card (was index 0) now sits last.
        assert config["views"][0]["cards"][2]["content"] == "one"
        assert config["views"][0]["cards"][0]["type"] == "button"

    async def test_move_out_of_range_raises(self, fake_client, set_card):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await set_card(url_path="my-dash", view="0", card_index=7, position=1)

        assert "Card index 7 out of range" in _error_message(exc_info)
        assert "lovelace/config/save" not in _ws_types(fake_client)

    async def test_move_dry_run_previews_without_save(self, fake_client, set_card):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_card(
            url_path="my-dash", view="0", card_index=0, position=2, dry_run=True
        )

        assert result["dry_run"] is True
        assert result["summary"] == "Moved card from index 0 to 2"
        assert result["config"]["views"][0]["cards"][2]["content"] == "one"
        assert "lovelace/config/save" not in _ws_types(fake_client)
        assert fake_client.send_websocket_message.call_count == 2

    async def test_stale_config_hash_raises_conflict(
        self, fake_client, set_card, new_card
    ):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await set_card(
                url_path="my-dash",
                view="0",
                card=new_card,
                config_hash="stale-hash-value",
            )

        assert "Dashboard modified since last read (conflict)" in _error_message(
            exc_info
        )
        assert "lovelace/config/save" not in _ws_types(fake_client)


# -----------------------------------------------------------------------------
# ha_config_remove_card
# -----------------------------------------------------------------------------


class TestRemoveCard:
    @pytest.fixture
    def remove_card(self, fake_client):
        return DashboardConfigTools(fake_client).ha_config_remove_card

    async def test_remove_at_index(self, fake_client, remove_card):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await remove_card(url_path="my-dash", view="0", card_index=1)

        assert result == {
            "success": True,
            "url_path": "my-dash",
            "summary": "Removed card at index 1 (type=button)",
        }
        assert len(config["views"][0]["cards"]) == 2
        assert config["views"][0]["cards"][1]["type"] == "tile"

    async def test_remove_dry_run_previews_without_save(self, fake_client, remove_card):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await remove_card(
            url_path="my-dash", view="0", card_index=1, dry_run=True
        )

        assert result["dry_run"] is True
        assert result["summary"] == "Removed card at index 1 (type=button)"
        assert len(result["config"]["views"][0]["cards"]) == 2
        assert "lovelace/config/save" not in _ws_types(fake_client)
        assert fake_client.send_websocket_message.call_count == 2

    async def test_remove_out_of_range_raises(self, fake_client, remove_card):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await remove_card(url_path="my-dash", view="0", card_index=9)

        assert "card_index 9 out of range" in _error_message(exc_info)
        assert "lovelace/config/save" not in _ws_types(fake_client)

    async def test_remove_from_sections_view(self, fake_client, remove_card):
        config = _config_with_sections_view()
        _stub_ws(fake_client, config)

        result = await remove_card(
            url_path="my-dash", view="0", section=0, card_index=0
        )

        assert result["summary"] == "Removed card at index 0 (type=tile)"
        assert config["views"][0]["sections"][0]["cards"] == []
        assert "lovelace/config/save" in _ws_types(fake_client)


# -----------------------------------------------------------------------------
# ha_config_list_view_sections
# -----------------------------------------------------------------------------


class TestListViewSections:
    @pytest.fixture
    def list_sections(self, fake_client):
        return DashboardConfigTools(fake_client).ha_config_list_view_sections

    async def test_lists_sections_with_indices_and_card_counts(
        self, fake_client, list_sections
    ):
        config = _config_with_sections_view()
        _stub_ws(fake_client, config)

        result = await list_sections(url_path="my-dash", view="0")

        assert result == {
            "success": True,
            "url_path": "my-dash",
            "sections": [
                {
                    "index": 0,
                    "title": "Climate",
                    "heading": "Climate zone",
                    "card_count": 1,
                }
            ],
        }
        # Read-only tool: exactly one config fetch, never a save.
        assert fake_client.send_websocket_message.call_count == 1
        assert "lovelace/config/save" not in _ws_types(fake_client)

    async def test_non_sections_view_raises(self, fake_client, list_sections):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await list_sections(url_path="my-dash", view="0")

        assert "not a 'sections'-type view" in _error_message(exc_info)


# -----------------------------------------------------------------------------
# ha_config_set_view — add new / merge existing
# -----------------------------------------------------------------------------


class TestSetView:
    @pytest.fixture
    def set_view(self, fake_client):
        return DashboardConfigTools(fake_client).ha_config_set_view

    @pytest.fixture
    def garage_view(self) -> dict:
        return {"title": "Garage", "path": "garage"}

    async def test_add_new_view_appends(self, fake_client, set_view, garage_view):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_view(url_path="my-dash", view_config=garage_view)

        assert result == {
            "success": True,
            "url_path": "my-dash",
            "summary": "Added new view at index 1",
        }
        assert config["views"][1] == garage_view
        assert "lovelace/config/save" in _ws_types(fake_client)

    async def test_add_new_view_at_position(self, fake_client, set_view, garage_view):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_view(url_path="my-dash", view_config=garage_view, position=0)

        assert result["summary"] == "Added new view at index 0"
        assert config["views"][0] == garage_view
        assert config["views"][1]["title"] == "Home"

    async def test_merge_into_existing_preserves_cards(self, fake_client, set_view):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_view(
            url_path="my-dash", view_config={"title": "Renamed"}, view="0"
        )

        assert result["summary"] == "Updated view '0'"
        view = config["views"][0]
        assert view["title"] == "Renamed"
        assert view["path"] == "home"
        assert len(view["cards"]) == 3  # cards preserved on shallow merge

    async def test_merge_with_cards_key_replaces(self, fake_client, set_view):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)
        replacement_cards = [{"type": "markdown", "content": "only"}]

        result = await set_view(
            url_path="my-dash", view_config={"cards": replacement_cards}, view="home"
        )

        assert result["summary"] == "Updated view 'home'"
        assert config["views"][0]["cards"] == replacement_cards
        assert len(config["views"][0]["cards"]) == 1

    async def test_set_view_dry_run_previews_without_save(
        self, fake_client, set_view, garage_view
    ):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await set_view(
            url_path="my-dash", view_config=garage_view, dry_run=True
        )

        assert result["dry_run"] is True
        assert result["summary"] == "Added new view at index 1"
        assert len(result["config"]["views"]) == 2
        assert "lovelace/config/save" not in _ws_types(fake_client)
        assert fake_client.send_websocket_message.call_count == 2

    async def test_bad_view_raises(self, fake_client, set_view):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await set_view(url_path="my-dash", view_config={"title": "X"}, view="ghost")

        assert "View 'ghost' not found" in _error_message(exc_info)
        assert "lovelace/config/save" not in _ws_types(fake_client)


# -----------------------------------------------------------------------------
# ha_config_remove_view
# -----------------------------------------------------------------------------


class TestRemoveView:
    @pytest.fixture
    def remove_view(self, fake_client):
        return DashboardConfigTools(fake_client).ha_config_remove_view

    async def test_remove_by_index_mentions_card_count(self, fake_client, remove_view):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await remove_view(url_path="my-dash", view="0")

        assert result == {
            "success": True,
            "url_path": "my-dash",
            "summary": "Removed view at index 0 and all 3 cards within it",
        }
        assert config["views"] == []

    async def test_remove_dry_run_previews_without_save(self, fake_client, remove_view):
        config = _config_with_flat_view()
        _stub_ws(fake_client, config)

        result = await remove_view(url_path="my-dash", view="0", dry_run=True)

        assert result["dry_run"] is True
        assert result["summary"] == "Removed view at index 0 and all 3 cards within it"
        assert result["config"]["views"] == []
        assert "lovelace/config/save" not in _ws_types(fake_client)
        assert fake_client.send_websocket_message.call_count == 2

    async def test_bad_view_raises(self, fake_client, remove_view):
        _stub_ws(fake_client, _config_with_flat_view())

        with pytest.raises(ToolError) as exc_info:
            await remove_view(url_path="my-dash", view="ghost")

        assert "View 'ghost' not found" in _error_message(exc_info)
        assert "lovelace/config/save" not in _ws_types(fake_client)
