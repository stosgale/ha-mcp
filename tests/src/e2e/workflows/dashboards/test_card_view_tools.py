"""
End-to-End tests for the card/view dashboard tools.

Validates the real-HA lifecycle of ha_config_set_card / ha_config_remove_card /
ha_config_list_view_sections / ha_config_set_view / ha_config_remove_view,
including dry_run previews that must not mutate state. Uses the same
fixture/cleanup pattern as test_lifecycle.py (unique hyphenated url_path,
best-effort cleanup delete, MCPAssertions/safe_call_tool helpers).
"""

import logging
import uuid

from ...utilities.assertions import MCPAssertions, extract_error_message, safe_call_tool

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TestCardViewTools:
    """E2E lifecycle for the card/view dashboard tools."""

    async def test_card_view_tool_lifecycle(self, mcp_client):
        """Create, mutate cards, list sections, add/merge/remove views, delete."""
        logger.info("Starting card/view tool lifecycle test")
        mcp = MCPAssertions(mcp_client)

        # Unique hyphenated url_path — new dashboards require a hyphen, and a
        # random suffix keeps parallel workers (-n2) from colliding.
        url_path = f"card-tools-e2e-{uuid.uuid4().hex[:8]}"

        try:
            # 1. Create dashboard with BOTH a sections view and a flat view.
            create_data = await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": url_path,
                    "title": "Card Tools E2E",
                    "config": {
                        "views": [
                            {
                                "title": "Sections View",
                                "path": "sections",
                                "type": "sections",
                                "sections": [
                                    {
                                        "title": "Section One",
                                        "heading": "Heading One",
                                        "cards": [
                                            {"type": "markdown", "content": "s1"}
                                        ],
                                    },
                                    {
                                        "title": "Section Two",
                                        "cards": [
                                            {"type": "markdown", "content": "s2"},
                                            {"type": "markdown", "content": "s3"},
                                        ],
                                    },
                                ],
                            },
                            {
                                "title": "Flat View",
                                "path": "flat",
                                "cards": [{"type": "markdown", "content": "flat-0"}],
                            },
                        ]
                    },
                },
            )
            assert create_data["success"] is True
            assert create_data["dashboard_created"] is True

            def _flat_card_contents(payload: dict) -> list[str]:
                views = payload["config"]["views"]
                flat = next(v for v in views if v.get("path") == "flat")
                return [c.get("content") for c in flat.get("cards", [])]

            # 10. dry_run previews: must return dry_run: True AND leave the
            # dashboard config byte-unchanged (no write, no backup snapshot).
            dry_insert = await safe_call_tool(
                mcp_client,
                "ha_config_set_card",
                {
                    "url_path": url_path,
                    "view": "flat",
                    "card": {"type": "markdown", "content": "dry-run-probe"},
                    "dry_run": True,
                },
            )
            assert dry_insert.get("dry_run") is True
            assert "summary" in dry_insert and "config" in dry_insert

            dry_remove = await safe_call_tool(
                mcp_client,
                "ha_config_remove_card",
                {
                    "url_path": url_path,
                    "view": "flat",
                    "card_index": 0,
                    "dry_run": True,
                },
            )
            assert dry_remove.get("dry_run") is True

            before_dry_run = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            assert _flat_card_contents(before_dry_run) == ["flat-0"]

            # 2. Insert a card into the flat view, then verify via re-read.
            insert = await mcp.call_tool_success(
                "ha_config_set_card",
                {
                    "url_path": url_path,
                    "view": "flat",
                    "card": {"type": "markdown", "content": "inserted"},
                },
            )
            assert insert["success"] is True

            after_insert = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            assert _flat_card_contents(after_insert) == ["flat-0", "inserted"]

            # 3. Replace the card at index 0.
            replace = await mcp.call_tool_success(
                "ha_config_set_card",
                {
                    "url_path": url_path,
                    "view": "flat",
                    "card_index": 0,
                    "card": {"type": "markdown", "content": "replaced"},
                },
            )
            assert replace["success"] is True

            after_replace = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            assert _flat_card_contents(after_replace) == ["replaced", "inserted"]

            # 4. Move the card at index 1 ("inserted") to position 0.
            move = await mcp.call_tool_success(
                "ha_config_set_card",
                {"url_path": url_path, "view": "flat", "card_index": 1, "position": 0},
            )
            assert move["success"] is True

            after_move = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            assert _flat_card_contents(after_move) == ["inserted", "replaced"]

            # 5. Remove the card at index 1 ("replaced").
            removed = await mcp.call_tool_success(
                "ha_config_remove_card",
                {"url_path": url_path, "view": "flat", "card_index": 1},
            )
            assert removed["success"] is True

            after_remove = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            assert _flat_card_contents(after_remove) == ["inserted"]

            # 6. List sections on the sections view; assert the flat view errors.
            sections = await mcp.call_tool_success(
                "ha_config_list_view_sections",
                {"url_path": url_path, "view": "sections"},
            )
            assert sections["success"] is True
            assert sections["sections"] == [
                {
                    "index": 0,
                    "title": "Section One",
                    "heading": "Heading One",
                    "card_count": 1,
                },
                {
                    "index": 1,
                    "title": "Section Two",
                    "heading": None,
                    "card_count": 2,
                },
            ]

            flat_error = await safe_call_tool(
                mcp_client,
                "ha_config_list_view_sections",
                {"url_path": url_path, "view": "flat"},
            )
            assert flat_error["success"] is False
            assert "sections" in extract_error_message(flat_error).lower()

            # 7a. Add a new view (view omitted → append).
            add_view = await mcp.call_tool_success(
                "ha_config_set_view",
                {
                    "url_path": url_path,
                    "view_config": {
                        "title": "New View",
                        "path": "new",
                        "cards": [{"type": "markdown", "content": "nv"}],
                    },
                },
            )
            assert add_view["success"] is True

            after_add_view = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            assert [v.get("path") for v in after_add_view["config"]["views"]] == [
                "sections",
                "flat",
                "new",
            ]

            # 7b. Merge into an existing view — cards preserved (view_config
            # carries no 'cards' key, so the shallow merge keeps them).
            merge_view = await mcp.call_tool_success(
                "ha_config_set_view",
                {
                    "url_path": url_path,
                    "view_config": {"title": "Flat View Renamed"},
                    "view": "flat",
                },
            )
            assert merge_view["success"] is True

            after_merge = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            merged_flat = next(
                v for v in after_merge["config"]["views"] if v.get("path") == "flat"
            )
            assert merged_flat["title"] == "Flat View Renamed"
            assert [c.get("content") for c in merged_flat.get("cards", [])] == [
                "inserted"
            ]

            # 8. Remove the added view.
            remove_view = await mcp.call_tool_success(
                "ha_config_remove_view", {"url_path": url_path, "view": "new"}
            )
            assert remove_view["success"] is True

            after_remove_view = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": url_path}
            )
            assert [v.get("path") for v in after_remove_view["config"]["views"]] == [
                "sections",
                "flat",
            ]

            # 9. Delete the dashboard (confirm=True) and verify it is gone.
            delete_data = await mcp.call_tool_success(
                "ha_config_delete_dashboard",
                {"url_path": url_path, "confirm": True},
            )
            assert delete_data["success"] is True

            list_after = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"list_only": True}
            )
            assert not any(
                d.get("url_path") == url_path for d in list_after.get("dashboards", [])
            )

            logger.info("Card/view tool lifecycle test completed successfully")
        finally:
            # Best-effort cleanup — already deleted on the happy path; the
            # safe_call swallows RESOURCE_NOT_FOUND on an early failure.
            await safe_call_tool(
                mcp_client,
                "ha_config_delete_dashboard",
                {"url_path": url_path, "confirm": True},
            )
