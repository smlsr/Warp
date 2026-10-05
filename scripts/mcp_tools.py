#!/usr/bin/env python3
"""Tool names Warp asks an agent to call.

Server names (`slackMcp`, `teamsMcp`, `jiraMcp`, `githubMcp`, `bitbucketMcp`)
live in `.warp/config.yaml`. This module is the tool names on those servers.
Herald, Jira, and `/warp-allow-notify` import these tuples so the names are
written down once.

Slack and Teams connectors do not share one tool name. The tuples list the
specific post tools Warp knows about. A different name goes in `notifyAllow`
as `server:tool`. Bitbucket's pull-request comment tool is not named here.
"""

from __future__ import annotations

# Official Atlassian remote MCP. `jiraMcp` (default "atlassian") is the server.
TOOL_RESOURCES = "getAccessibleAtlassianResources"
TOOL_ISSUE = "getJiraIssue"
TOOL_TRANSITIONS = "getTransitionsForJiraIssue"
TOOL_TRANSITIONS_ALT = "listJiraIssueTransitions"
TOOL_TRANSITION = "transitionJiraIssue"
TOOL_COMMENT = "addOrEditJiraIssueComment"
TOOL_COMMENT_ALT = "addCommentToJiraIssue"
TOOL_SEARCH = "searchJiraIssuesUsingJql"
TOOL_FIELDS = "getJiraProjectIssueTypesMetadata"
TOOL_REMOTE_LINKS = "getJiraIssueRemoteIssueLinks"
TOOL_PROJECTS = "getVisibleJiraProjects"
TOOL_EDIT = "editJiraIssue"
TOOL_EDITMETA = "getJiraIssueEditmeta"
# Screen tools exist on Rovo (manage_jira). They add a field that already exists.
TOOL_SCREEN = "getJiraScreen"
TOOL_SCREEN_UPDATE = "updateJiraScreen"
# Remote-link create exists on Rovo write_jira. Lookup matches globalId warp:<id>.
TOOL_REMOTE_CREATE = "createJiraIssueRemoteIssueLink"
# Not in the Rovo catalog (30 Sep 2026). Listed so a future create-field tool is allowed.
# POST /rest/api/3/field and field contexts are not exposed. Probe these names; do not invent a call.
TOOL_CREATE_FIELD = "createJiraField"
TOOL_CREATE_FIELD_ALT = "createCustomField"
TOOL_CREATE_FIELD_ALT2 = "createJiraCustomField"

JIRA_TOOLS = (
    TOOL_RESOURCES,
    TOOL_ISSUE,
    TOOL_TRANSITIONS,
    TOOL_TRANSITIONS_ALT,
    TOOL_TRANSITION,
    TOOL_COMMENT,
    TOOL_COMMENT_ALT,
    TOOL_SEARCH,
    TOOL_FIELDS,
    TOOL_REMOTE_LINKS,
    TOOL_PROJECTS,
    TOOL_EDIT,
    TOOL_EDITMETA,
    TOOL_SCREEN,
    TOOL_SCREEN_UPDATE,
    TOOL_REMOTE_CREATE,
    TOOL_CREATE_FIELD,
    TOOL_CREATE_FIELD_ALT,
    TOOL_CREATE_FIELD_ALT2,
)

CREATE_FIELD_TOOLS = (
    TOOL_CREATE_FIELD,
    TOOL_CREATE_FIELD_ALT,
    TOOL_CREATE_FIELD_ALT2,
)

# GitHub pull-request comment. `gh pr comment` is a CLI fallback, not an MCP tool.
GITHUB_COMMENT = "add_issue_comment"
GITHUB_TOOLS = (GITHUB_COMMENT,)

# Channel post tools, plus the reads Herald uses for warp:status.
# slack_read_channel, slack_read_thread, and slack_search_channels are the
# hosted Slack MCP names. Teams uses the same three jobs under teams_ names.
# A connector that spells them differently goes in notifyAllow as server:tool.
SLACK_TOOLS = (
    "slack_post_message",
    "slack_send_message",
    "slack_read_channel",
    "slack_read_thread",
    "slack_search_channels",
)
TEAMS_TOOLS = (
    "send_channel_message",
    "teams_send_message",
    "teams_read_channel",
    "teams_read_thread",
    "teams_search_channels",
)

# Named in docs so a reader can see it, and not called by any Warp flow.
# The coverage test requires every referenced tool to be allowed or listed here.
EXCLUDED_TOOLS = {
    "lookupJiraAccountId": "Not called by any Warp flow.",
}


def warp_tool_names(*, with_jira: bool = True, with_git: bool = True) -> set[str]:
    """Tool names /warp-allow-notify writes. Server ids are separate."""
    names = set(SLACK_TOOLS) | set(TEAMS_TOOLS)
    if with_jira:
        names |= set(JIRA_TOOLS)
    if with_git:
        names |= set(GITHUB_TOOLS)
    return names
