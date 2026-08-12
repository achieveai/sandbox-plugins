// =============================================================================
// ADO API Client — zero dependencies, transport via the bundled ado-cli.js
//
// All Azure DevOps API calls go through this module. Every call is a
// spawned invocation of `${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js <method> --structured`.
// Auth: none — sandbox-auth:azure-devops is the only auth path. No PAT, no
// Bearer token, no MCP server. See callAdoCli() below.
// =============================================================================

import { execSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ---------------------------------------------------------------------------
// CLI transport
// ---------------------------------------------------------------------------

const ADO_CLI_PATH = path.resolve(
  fileURLToPath(import.meta.url),
  "..", "..", "..", "..", "scripts", "ado-cli.js"
);

function callAdoCli(method, params) {
  return new Promise((resolve, reject) => {
    const child = spawn("node", [ADO_CLI_PATH, method, "--structured"], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; }); // captured for diagnostics only — never treated as failure
    child.on("close", (code) => {
      if (code === 0) {
        try { resolve(JSON.parse(stdout)); }
        catch (e) { reject(new Error(`ado-cli.js returned non-JSON stdout for ${method}: ${stdout.slice(0, 500)}`)); }
      } else {
        reject(new Error(`ado-cli.js ${method} exited ${code}: ${stdout || stderr}`));
      }
    });
    child.stdin.write(JSON.stringify(params));
    child.stdin.end();
  });
}

// ---------------------------------------------------------------------------
// Config resolution
// ---------------------------------------------------------------------------

export function resolveConfig(repoRoot) {
  let orgUrl = process.env.AZURE_DEVOPS_ORG_URL;
  let project = process.env.AZURE_DEVOPS_PROJECT;
  let repository = process.env.AZURE_DEVOPS_REPOSITORY;

  if (!orgUrl || !project) {
    try {
      const remote = execSync("git remote get-url origin", {
        cwd: repoRoot, encoding: "utf-8",
      }).trim();
      const parsed = parseGitRemote(remote);
      if (parsed) {
        orgUrl = orgUrl || parsed.orgUrl;
        project = project || parsed.project;
        repository = repository || parsed.repository;
      }
    } catch { /* ignore */ }
  }

  if (!orgUrl || !project) {
    throw new Error(
      "Set AZURE_DEVOPS_ORG_URL and AZURE_DEVOPS_PROJECT, or use a git repo with an ADO remote."
    );
  }

  return { orgUrl, project, repository: repository || "" };
}

function parseGitRemote(url) {
  let m;
  // https://dev.azure.com/{org}/{project}/_git/{repo}
  m = url.match(/https?:\/\/dev\.azure\.com\/([^/]+)\/([^/]+)\/_git\/(.+?)(?:\.git)?$/);
  if (m) return { orgUrl: `https://dev.azure.com/${m[1]}`, project: m[2], repository: m[3] };

  // https://{org}.visualstudio.com/{project}/_git/{repo}
  m = url.match(/https?:\/\/([^.]+)\.visualstudio\.com\/([^/]+)\/_git\/(.+?)(?:\.git)?$/);
  if (m) return { orgUrl: `https://${m[1]}.visualstudio.com`, project: m[2], repository: m[3] };

  // SSH variants
  m = url.match(/git@ssh\.dev\.azure\.com:v3\/([^/]+)\/([^/]+)\/(.+?)$/);
  if (m) return { orgUrl: `https://dev.azure.com/${m[1]}`, project: m[2], repository: m[3] };

  m = url.match(/[^@]+@vs-ssh\.visualstudio\.com:v3\/([^/]+)\/([^/]+)\/(.+?)$/);
  if (m) return { orgUrl: `https://dev.azure.com/${m[1]}`, project: m[2], repository: m[3] };

  return null;
}

export function getDevIdentity(cwd) {
  const name = execSync("git config user.name", { cwd, encoding: "utf-8" }).trim();
  const email = execSync("git config user.email", { cwd, encoding: "utf-8" }).trim();
  return { name, email };
}

// ---------------------------------------------------------------------------
// Sprint
// ---------------------------------------------------------------------------

export async function getCurrentSprint(orgUrl, project, team) {
  // The CLI's getCurrentSprint resolves the default team internally when no
  // teamId is given. This replaces the source's client-side team-name-candidate
  // guessing ([team] or [project, "${project} Team"]) — the CLI's own default-team
  // resolution is the more robust equivalent.
  let sprint = await callAdoCli("getCurrentSprint", team ? { teamId: team } : {});

  if (!sprint) {
    // Last resort: discover a team and retry once (mirrors source's
    // list-teams-and-use-first-one fallback).
    try {
      const teams = await callAdoCli("getTeams", {});
      const list = Array.isArray(teams) ? teams : [];
      if (list.length > 0) {
        console.error(`[API] Using discovered team: "${list[0].name}"`);
        sprint = await callAdoCli("getCurrentSprint", { teamId: list[0].id });
      }
    } catch { /* ignore */ }
  }

  if (!sprint || !sprint.path) {
    throw new Error("No sprint iterations found. Check team/project config.");
  }

  return {
    path: sprint.path,
    startDate: sprint.attributes?.startDate || "",
    endDate: sprint.attributes?.finishDate || "",
  };
}

// ---------------------------------------------------------------------------
// Work item query
// ---------------------------------------------------------------------------

export async function querySprintWorkItems(orgUrl, project, sprintPath) {
  const safePath = sprintPath.replace(/'/g, "''");
  const wiql = `
    SELECT [System.Id]
    FROM WorkItems
    WHERE [System.AssignedTo] = @Me
      AND [System.IterationPath] UNDER '${safePath}'
      AND [System.WorkItemType] IN ('Bug', 'Task', 'User Story')
      AND [System.State] NOT IN ('Closed', 'Done', 'Removed', 'Resolved')
    ORDER BY [System.ChangedDate] DESC
  `;

  // days: 3650 (~10y) overrides listWorkItems' default 7-day recency window —
  // the source WIQL had no date bound, it scoped purely by IterationPath/State.
  const data = await callAdoCli("listWorkItems", { query: wiql, top: 100, days: 3650 });
  const ids = (data?.workItems || []).map((wi) => wi.id);
  if (ids.length > 0) return ids;

  // Fallback: stale sprint — query by state only
  console.error(`[API] No items in sprint "${sprintPath}", falling back to state query.`);
  const fallbackWiql = `
    SELECT [System.Id]
    FROM WorkItems
    WHERE [System.AssignedTo] = @Me
      AND [System.WorkItemType] IN ('Bug', 'Task', 'User Story')
      AND [System.State] IN ('New', 'Active')
    ORDER BY [System.ChangedDate] DESC
  `;

  const fallback = await callAdoCli("listWorkItems", { query: fallbackWiql, top: 100, days: 3650 });
  return (fallback?.workItems || []).map((wi) => wi.id);
}

// ---------------------------------------------------------------------------
// Work Items
// ---------------------------------------------------------------------------

export async function fetchWorkItemChangedDate(orgUrl, project, id) {
  const batch = await callAdoCli("getWorkItemsBatch", { ids: [id], fields: ["System.ChangedDate"] });
  const item = (Array.isArray(batch) ? batch : [])[0];
  return item?.fields?.["System.ChangedDate"] || new Date(0).toISOString();
}

// Fields fetched raw (dotted System.*/Microsoft.VSTS.* keys preserved) via
// getWorkItemsBatch, merged with getWorkItemById's relations (reshaped back to
// the {rel, url} contract extractLinkedPrIds() expects). getWorkItemById's own
// curated output omits AcceptanceCriteria/raw fields, so it is used for
// relations only; getWorkItemsBatch is used for every field value.
const DETAIL_FIELDS = [
  "System.Title",
  "System.WorkItemType",
  "System.State",
  "System.AreaPath",
  "System.IterationPath",
  "System.Description",
  "Microsoft.VSTS.Common.AcceptanceCriteria",
  "Microsoft.VSTS.Common.Priority",
];

export async function fetchWorkItemDetails(orgUrl, project, id) {
  const [batch, byId] = await Promise.all([
    callAdoCli("getWorkItemsBatch", { ids: [id], fields: DETAIL_FIELDS }),
    callAdoCli("getWorkItemById", { id, fullDescription: true }),
  ]);

  const item = (Array.isArray(batch) ? batch : [])[0];
  const relations = (byId?.relations || []).map((r) => {
    if (r.type === "ArtifactLink") {
      return { rel: "ArtifactLink", url: r.artifactUri };
    }
    return { rel: r.type, relatedWorkItemId: r.relatedId };
  });

  return { id: item?.id ?? id, fields: item?.fields ?? {}, relations };
}

export async function fetchWorkItemComments(orgUrl, project, id) {
  const data = await callAdoCli("getWorkItemComments", { id, order: "asc", top: 200 });
  const comments = data?.comments || [];
  return comments.map((c) => ({
    id: c.id,
    text: c.text || "",
    createdDate: c.createdDate || "",
    modifiedDate: c.modifiedDate || "",
    createdBy: {
      displayName: c.createdBy?.displayName || "",
      uniqueName: c.createdBy?.uniqueName || "",
    },
  }));
}

export async function fetchWorkItemFull(orgUrl, project, id) {
  const [details, comments] = await Promise.all([
    fetchWorkItemDetails(orgUrl, project, id),
    fetchWorkItemComments(orgUrl, project, id),
  ]);
  return { details, comments };
}

// ---------------------------------------------------------------------------
// Work Item Relations
// ---------------------------------------------------------------------------

export function extractLinkedPrIds(relations) {
  const prIds = [];
  for (const rel of relations || []) {
    if (rel.rel !== "ArtifactLink" || !rel.url?.includes("Git/PullRequestId")) continue;
    const decoded = decodeURIComponent(rel.url);
    const match = decoded.match(/Git\/PullRequestId\/[^/]+\/[^/]+\/(\d+)/);
    if (match) prIds.push(parseInt(match[1], 10));
  }
  return prIds;
}

// ---------------------------------------------------------------------------
// Pull Requests
// ---------------------------------------------------------------------------

const VOTE_MAP = { 10: "approved", 5: "approvedWithSuggestions", 0: "noVote", "-5": "waitingForAuthor", "-10": "rejected" };

export async function fetchPrDetails(orgUrl, project, repository, prId) {
  const pr = await callAdoCli("getPullRequest", { repository, pullRequestId: prId });

  return {
    prId: pr.pullRequestId,
    title: pr.title || "",
    status: pr.status,  // 1=active, 2=abandoned, 3=completed
    sourceBranch: (pr.sourceRefName || "").replace("refs/heads/", ""),
    targetBranch: (pr.targetRefName || "").replace("refs/heads/", ""),
    lastSourceCommitId: pr.lastMergeSourceCommit?.commitId || "",
    mergeStatus: {
      hasConflicts: pr.mergeStatus === 2,
      status: String(pr.mergeStatus ?? "unknown"),
    },
    reviewerVotes: (pr.reviewers || []).map((r) => ({
      name: r.displayName || r.uniqueName || "unknown",
      vote: VOTE_MAP[r.vote] || `unknown(${r.vote})`,
    })),
  };
}

export async function isActivePr(orgUrl, project, repository, prId) {
  try {
    const pr = await fetchPrDetails(orgUrl, project, repository, prId);
    return pr.status === 1;
  } catch {
    return false;
  }
}

export async function fetchUnresolvedThreads(orgUrl, project, repository, prId) {
  const data = await callAdoCli("getPullRequestComments", { repository, pullRequestId: prId });
  const threads = data?.threads || [];
  const result = [];

  for (const thread of threads) {
    if (thread.status !== "active" && thread.status !== "pending") continue;  // Only active or pending
    // classification === "system" is the CLI's closest equivalent to the source's
    // CodeReviewAutoClosedByPushId/CodeReviewVoteUpdatedByIdentity system-thread filter.
    if (thread.classification === "system") continue;

    const comments = thread.comments || [];
    if (comments.length === 0) continue;

    result.push({
      threadId: thread.id,
      status: thread.status,
      filePath: thread.filePath || null,
      lineNumber: thread.lineStart ?? null,
      comments: comments.map((c) => ({
        author: c.author || "unknown",
        date: c.publishedDate || "",
        text: c.content || "",
      })),
    });
  }

  return result;
}

// ---------------------------------------------------------------------------
// Builds
// ---------------------------------------------------------------------------

export async function fetchBuildStatus(orgUrl, project, sourceBranch, repository) {
  const branchRef = `refs/heads/${sourceBranch}`;

  let builds;
  try {
    builds = await callAdoCli("getBuilds", {
      branchName: branchRef,
      repositoryId: repository,
      repositoryType: "TfsGit",
      top: 5,
      queryOrder: "startTimeDescending",
    });
  } catch {
    return [];
  }

  builds = Array.isArray(builds) ? builds : [];
  const result = [];
  let failureLogFetched = false;

  for (const build of builds) {
    const buildId = build.id;
    const status = build.status;  // 1=inProgress, 2=completed
    const buildResult = build.result; // 0=none, 2=succeeded, 8=failed, 32=canceled

    let resultStr = "unknown";
    if (status === 1) resultStr = "inProgress";
    else if (buildResult === 2) resultStr = "succeeded";
    else if (buildResult === 8) resultStr = "failed";
    else if (buildResult === 32) resultStr = "canceled";

    let failureSummary = null;

    // Fetch logs only for the most recent failed build
    if (resultStr === "failed" && !failureLogFetched) {
      failureSummary = await fetchBuildFailureLogs(orgUrl, project, buildId);
      failureLogFetched = true;
    }

    result.push({
      buildId,
      result: resultStr,
      definitionName: build.definition?.name || `build-${buildId}`,
      failureSummary,
    });
  }

  return result;
}

async function fetchBuildFailureLogs(orgUrl, project, buildId) {
  try {
    const timeline = await callAdoCli("getBuildTimeline", { buildId });

    const failedRecords = (timeline?.records || []).filter(
      (r) => r.result === "failed" && r.log?.id
    );
    if (failedRecords.length === 0) return null;

    const logId = failedRecords[0].log.id;

    // Get log metadata to know line count
    const logs = await callAdoCli("getBuildLog", { buildId });
    const logList = Array.isArray(logs) ? logs : [];
    const logEntry = logList.find((l) => l.id === logId);
    const lineCount = logEntry?.lineCount || 200;

    // Fetch last 150 lines
    const startLine = Math.max(1, lineCount - 150);
    const logContent = await callAdoCli("getBuildLog", {
      buildId, logId, startLine, endLine: lineCount,
    });

    const lines = logContent?.lines || [];
    return lines.length > 0 ? lines.join("\n") : null;
  } catch (err) {
    console.error(`[API] Failed to fetch build logs for build ${buildId}:`, err.message);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Full PR Context (combines all)
// ---------------------------------------------------------------------------

export async function fetchPrContext(orgUrl, project, repository, prId) {
  const details = await fetchPrDetails(orgUrl, project, repository, prId);
  const [unresolvedThreads, builds] = await Promise.all([
    fetchUnresolvedThreads(orgUrl, project, repository, prId),
    fetchBuildStatus(orgUrl, project, details.sourceBranch, repository),
  ]);
  return { details, unresolvedThreads, builds };
}
