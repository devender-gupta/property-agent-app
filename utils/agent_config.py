from pathlib import Path
from typing import Any, Callable

import yaml
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient

from tools.property_tools import search_properties_api
from tools.scheduling_tools import (
    cancel_tour_booking_api,
    confirm_tour_booking,
    list_customer_bookings_api,
    reschedule_tour_booking_api,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "search_properties_api": search_properties_api,
    "confirm_tour_booking": confirm_tour_booking,
    "list_customer_bookings_api": list_customer_bookings_api,
    "cancel_tour_booking_api": cancel_tour_booking_api,
    "reschedule_tour_booking_api": reschedule_tour_booking_api,
}


def load_agent_config(config_name: str) -> dict[str, Any]:
    config_path = CONFIG_DIR / config_name
    with config_path.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    required_keys = {"name", "description", "system_prompt", "tools"}
    missing_keys = required_keys - set(config.keys())
    if missing_keys:
        raise ValueError(f"Missing required keys in {config_path.name}: {sorted(missing_keys)}")

    if not isinstance(config["tools"], list):
        raise ValueError(f"Invalid tools definition in {config_path.name}: expected a list")

    return config


def resolve_tools(tool_entries: list[dict[str, Any]], config_name: str) -> list[Callable[..., Any]]:
    resolved: list[Callable[..., Any]] = []
    for tool_entry in tool_entries:
        tool_name = tool_entry.get("name") if isinstance(tool_entry, dict) else None
        if not tool_name:
            raise ValueError(f"Invalid tool entry in {config_name}: {tool_entry}")

        tool_callable = TOOL_REGISTRY.get(tool_name)
        if tool_callable is None:
            raise ValueError(f"Unknown tool '{tool_name}' in {config_name}")

        resolved.append(tool_callable)

    return resolved


def build_agent_from_config(config_name: str, client: OpenAIChatClient) -> Agent:
    config = load_agent_config(config_name)
    tools = resolve_tools(config["tools"], config_name)

    return Agent(
        name=config["name"],
        description=config["description"],
        client=client,
        tools=tools,
        require_per_service_call_history_persistence=True,
        instructions=config["system_prompt"],
    )
