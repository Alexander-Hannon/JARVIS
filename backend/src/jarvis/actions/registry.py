"""Action-type registry: the single allowlist of external writes JARVIS can ever execute.

Each ActionType declares how the executor runs and checks it. Model output can only *name* a registered type inside a
proposal; whether it runs is decided by approvals/policy + executor guards in application code.

Tiers (the autonomy floor of an action type — a policy rule can never lower an action below its floor):
- T1: eligible for auto-approval by an AJ-authored policy rule (low-risk, reversible, affects only AJ).
- T2: always needs AJ's explicit approval (reaches other people, deletes, money, physical security, network egress).
- T3: forbidden — never proposable.

Idempotency:
- native:  provider honors an idempotency key (Todoist command uuid, deterministic calendar event id).
- marker:  a marker is embedded and searched for before any retry.
- none:    no way to reconcile; an unknown outcome always goes to AJ and is never retried automatically.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal


class Tier(IntEnum):
    T1 = 1
    T2 = 2
    T3 = 3


Idem = Literal["native", "marker", "none"]


@dataclass(frozen=True)
class ActionType:
    name: str
    provider: str                     # stored on proposed_actions.provider
    handler: str                      # "module:function", resolved lazily to avoid import cycles
    tier: Tier
    idempotency: Idem
    label: str                        # human phrasing for cards, Help and Policies
    account_provider: str | None = None   # integration_accounts.provider that must still be connected (None = local)
    grant_type: str | None = None         # scope_grants.resource_type needing a `write` grant
    grant_always: bool = False            # require the grant even when no target_resource is set
    undo: str | None = None               # name of the inverse action type, when reversible
    reaches_others: bool = False          # visible to anyone besides AJ (send/post/invite) => hard T2 floor
    # V5 org-impacting types: after the owner approves, a *different* person holding `co_approver_perm` must co-approve
    # (the executor re-checks both). Such types can never be T1.
    co_approver_perm: str | None = None

    def resolve_handler(self) -> Callable:
        mod, fn = self.handler.split(":")
        return getattr(importlib.import_module(mod), fn)


_EX = "jarvis.actions.executor"
REGISTRY: dict[str, ActionType] = {}


def register(at: ActionType) -> ActionType:
    if at.reaches_others and at.tier < Tier.T2:
        raise ValueError(f"{at.name}: actions that reach other people can never be T1")
    if at.co_approver_perm and at.tier < Tier.T2:
        raise ValueError(f"{at.name}: org-impacting actions need explicit approvals and can never be T1")
    if at.name in REGISTRY and REGISTRY[at.name] != at:
        raise ValueError(f"conflicting registration for {at.name}")
    REGISTRY[at.name] = at
    return at


for _at in (
    ActionType("task.create", "todoist", f"{_EX}:h_task_create", Tier.T1, "native", "Create a Todoist task",
               "todoist", "todoist_project", grant_always=True, undo="task.delete"),
    ActionType("task.update", "todoist", f"{_EX}:h_task_update", Tier.T1, "native", "Edit a Todoist task",
               "todoist", "todoist_project"),
    ActionType("task.complete", "todoist", f"{_EX}:h_task_complete", Tier.T1, "native", "Complete a Todoist task",
               "todoist", "todoist_project", undo="task.reopen"),
    ActionType("task.reopen", "todoist", f"{_EX}:h_task_reopen", Tier.T1, "native", "Reopen a Todoist task",
               "todoist", "todoist_project", undo="task.complete"),
    ActionType("task.delete", "todoist", f"{_EX}:h_task_delete", Tier.T2, "native", "Delete a Todoist task",
               "todoist", "todoist_project"),
    ActionType("event.create", "google_calendar", f"{_EX}:h_event_create", Tier.T1, "native",
               "Add a personal calendar block", "google", "calendar", grant_always=True, undo="event.delete"),
    ActionType("event.update", "google_calendar", f"{_EX}:h_event_update", Tier.T1, "native", "Move/edit a calendar event",
               "google", "calendar", grant_always=True),
    ActionType("event.delete", "google_calendar", f"{_EX}:h_event_delete", Tier.T2, "native", "Delete a calendar event",
               "google", "calendar", grant_always=True),
    ActionType("file.write", "local_fs", f"{_EX}:h_file", Tier.T1, "marker", "Write a file in a workspace folder"),
    ActionType("file.move", "local_fs", f"{_EX}:h_file", Tier.T2, "marker", "Move/rename a workspace file"),
):
    register(_at)

_MAIL = "jarvis.actions.handlers_mail"
for _box, _acct, _name in (("gmail", "gmail", "Gmail"), ("outlook", "microsoft", "Outlook")):
    register(ActionType(f"{_box}.draft.create", _box, f"{_MAIL}:h_draft_create", Tier.T1, "marker",
                        f"Save a {_name} draft (not sent)", _acct, undo=f"{_box}.draft.delete"))
    register(ActionType(f"{_box}.draft.delete", _box, f"{_MAIL}:h_draft_delete", Tier.T2, "native",
                        f"Delete a {_name} draft", _acct))
    # Sending reaches other people: hard T2, needs the mail_send grant AJ creates by enabling sending, never retried.
    register(ActionType(f"{_box}.send", _box, f"{_MAIL}:h_send", Tier.T2, "none", f"Send an email from {_name}", _acct,
                        "mail_send", grant_always=True, reaches_others=True))


_APPS = "jarvis.actions.handlers_apps"
for _at in (
    ActionType("notion.page.create", "notion", f"{_APPS}:h_notion_create", Tier.T1, "marker",
               "Create a Notion page under a page you chose", "notion", "notion_page", grant_always=True),
    ActionType("notion.page.append", "notion", f"{_APPS}:h_notion_append", Tier.T1, "none",
               "Append a note to a Notion page you chose", "notion", "notion_page", grant_always=True),
    ActionType("slack.post_self", "slack", f"{_APPS}:h_slack_post", Tier.T1, "marker", "Post to your own Slack DM",
               "slack", "slack_self", grant_always=True),
    ActionType("slack.post", "slack", f"{_APPS}:h_slack_post", Tier.T2, "marker", "Post a Slack message as you",
               "slack", "slack_channel", grant_always=True, reaches_others=True),
    ActionType("music.play", "spotify", f"{_APPS}:h_music", Tier.T1, "native", "Play music on Spotify", "spotify"),
    ActionType("music.pause", "spotify", f"{_APPS}:h_music", Tier.T1, "native", "Pause Spotify", "spotify"),
    ActionType("music.next", "spotify", f"{_APPS}:h_music", Tier.T1, "none", "Skip to the next track", "spotify"),
    ActionType("music.queue", "spotify", f"{_APPS}:h_music", Tier.T1, "none", "Queue a track on Spotify", "spotify"),
    ActionType("home.control", "homeassistant", f"{_APPS}:h_home", Tier.T1, "native",
               "Control a comfort device (lights, switches, scenes, media, climate)", "homeassistant", "ha_entity",
               grant_always=True),
    # Physical security: locks, alarms, garage doors, scripts/automations — never automatable.
    ActionType("home.secure", "homeassistant", f"{_APPS}:h_home", Tier.T2, "native",
               "Operate a security device (lock, alarm, garage door, script)", "homeassistant", "ha_entity",
               grant_always=True),
):
    register(_at)


# MCP tools AJ reviewed as "propose". Which type a call gets is decided in code from AJ's review (never by the model),
# and re-checked by the handler at execution time.
register(ActionType("mcp.call", "mcp", "jarvis.actions.handlers_mcp:h_mcp_call", Tier.T2, "none",
                    "Run an action tool on one of your MCP servers"))
register(ActionType("mcp.call.auto", "mcp", "jarvis.actions.handlers_mcp:h_mcp_call", Tier.T1, "none",
                    "Run an MCP tool you marked as safe to automate"))


# Network egress for the sandbox is a hard T2: always AJ's explicit approval, limited domains, auto-expiring.
register(ActionType("sandbox.network", "sandbox", "jarvis.actions.handlers_sandbox:h_network", Tier.T2, "native",
                    "Open the sandbox's internet access to specific domains for a while"))


# A skill becomes standing instructions for every user's agent: the proposer reviews the exact text, then an admin
# co-approves it (org-wide change). An admin's own proposal still needs a second admin.
register(ActionType("skill.install", "skills", "jarvis.actions.handlers_sandbox:h_skill_install", Tier.T2, "native",
                    "Install a skill (saved instructions JARVIS will follow)", co_approver_perm="org.integrations"))


def get(name: str) -> ActionType | None:
    return REGISTRY.get(name)


def provider_for(name: str) -> str | None:
    at = REGISTRY.get(name)
    return at.provider if at and at.tier < Tier.T3 else None


def allowed() -> dict[str, str]:
    """name -> provider for every executable type (the old ALLOWED_ACTIONS shape)."""
    return {n: a.provider for n, a in REGISTRY.items() if a.tier < Tier.T3}
