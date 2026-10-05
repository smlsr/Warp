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
)

# GitHub pull-request comment. `gh pr comment` is a CLI fallback, not an MCP tool.
GITHUB_COMMENT = "add_issue_comment"
GITHUB_TOOLS = (GITHUB_COMMENT,)

# Channel post tools. Both names are specific tools; neither entry is a wildcard.
SLACK_TOOLS = ("slack_post_message", "slack_send_message")
TEAMS_TOOLS = ("send_channel_message", "teams_send_message")
