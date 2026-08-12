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

// The CLI's one enforceable auth invariant (mutation_privacy_policy bullet 5): auth
// initializes, and this exact line prints to stderr, before any method runs — guaranteed
// present on every exit-0 call. A non-zero exit is not guaranteed to carry it (e.g. exit 4,
// missing env vars, fails before auth initializes), which is fine — only the success path is
// validated below. It never carries a credential value (auth type is a mode name —
// "none"/"pat"/"entra"/"azcli"/"interactive" — not a token). Matched, not required to be the
// *whole* stderr contents — stderr also always carries Node's DEP0169 warning, which is not a
// failure signal and is never inspected here.
const AUTH_BANNER = /\[Auth\] Auth type: (\S+), PAT: (set|not set)/;

// Distinct from ordinary transport/exit-code errors so every fallback path below can single
// it out and rethrow instead of silently falling back — an auth leak must never be treated as
// just another failed call. The message is a fixed, generic string: it never echoes the raw
// banner or stderr, so nothing credential-adjacent can reach a log line through this path.
export class AuthInvariantError extends Error {
  constructor(method) {
    super(`ado-cli.js ${method}: auth invariant violated on a successful call (redacted — see AUTH_BANNER in ado-api.mjs)`);
    this.name = "AuthInvariantError";
  }
}

// Single choke point used by every catch below (and by scan.mjs's per-item and per-scan catches)
// that would otherwise treat "the call failed" and "the call succeeded but leaked a credential"
// the same way. Non-auth errors fall through untouched so ordinary fallback behavior (return
// null/[]/false, try the next thing, count toward a retry/error cap) is unchanged. Exported so
// callers outside this module reuse this exact check instead of duplicating an `instanceof`.
export function rethrowIfAuthInvariant(err) {
  if (err instanceof AuthInvariantError) throw err;
}

// The CLI reads AZURE_DEVOPS_ORG_URL / AZURE_DEVOPS_PROJECT from its own environment and
// from nothing else: `invocationConfig` consults only those two variables before
// `fail(…, 4)`, and `parseArgs` exposes no --org/--project flag, so no stdin parameter
// can carry them. Without this forwarding the child would ignore resolveConfig()
// entirely and the advertised git-remote auto-detection would be dead — resolveConfig
// would succeed and every CLI call would still exit 4.
//
// process.env is spread first so the child keeps everything the parent had — notably
// HTTP_PROXY/HTTPS_PROXY/NO_PROXY and NODE_EXTRA_CA_CERTS/SSL_CERT_FILE, which the
// sandbox egress proxy requires. Only the two config variables are overridden, and only
// when the caller actually resolved a value, so an unset argument never blanks an
// inherited one. Nothing here is a credential: sandbox-auth injects that server-side.
function buildChildEnv(config) {
  const env = { ...process.env };
  if (config?.orgUrl) env.AZURE_DEVOPS_ORG_URL = config.orgUrl;
  if (config?.project) env.AZURE_DEVOPS_PROJECT = config.project;
  return env;
}

// `config` carries the {orgUrl, project} each exported function already receives, so the
// values reach the child without any public signature changing.
function callAdoCli(method, params, config) {
  return new Promise((resolve, reject) => {
    // `error` and `close` are not mutually exclusive, and scan.mjs runs these under a
    // bounded-concurrency Promise.allSettled — a promise that settles twice (or never)
    // either double-releases or permanently holds a semaphore permit. Settle exactly once.
    let settled = false;
    const settle = (fn, value) => {
      if (settled) return;
      settled = true;
      fn(value);
    };

    let child;
    try {
      child = spawn("node", [ADO_CLI_PATH, method, "--structured"], {
        stdio: ["pipe", "pipe", "pipe"],
        env: buildChildEnv(config),
      });
    } catch (err) {
      settle(reject, new Error(`failed to spawn ado-cli.js for ${method}: ${err.message}`));
      return;
    }

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    // Buffered for diagnostics and for the auth-banner check below — never treated as a
    // failure signal merely for being non-empty (see `close` handler).
    child.stderr.on("data", (d) => { stderr += d; });

    // Spawn failures (e.g. ENOENT when `node` is not on PATH) emit `error` and never
    // emit `close`. Without this handler the promise would hang forever and the
    // unhandled 'error' event would crash the process.
    child.on("error", (err) => {
      settle(reject, new Error(`failed to spawn ado-cli.js for ${method}: ${err.message}`));
    });

    child.on("close", (code) => {
      if (code === 0) {
        // Fail closed if the success path didn't report the expected auth invariant —
        // this is the only stderr content ever branched on; ordinary non-empty stderr
        // (e.g. the DEP0169 warning) is not inspected and is not a failure.
        const banner = stderr.match(AUTH_BANNER);
        if (!banner || banner[1] !== "none" || banner[2] !== "not set") {
          settle(reject, new AuthInvariantError(method));
          return;
        }
        let parsed;
        try {
          parsed = JSON.parse(stdout);
        } catch {
          settle(reject, new Error(`ado-cli.js returned non-JSON stdout for ${method}: ${stdout.slice(0, 500)}`));
          return;
        }
        settle(resolve, parsed);
      } else {
        settle(reject, new Error(`ado-cli.js ${method} exited ${code}: ${stdout || stderr}`));
      }
    });

    // The CLI exits before draining stdin whenever AZURE_DEVOPS_ORG_URL /
    // AZURE_DEVOPS_PROJECT are unset, which surfaces here as an EPIPE/ECONNRESET
    // stream error. Swallow it — `close` still fires and reports the real exit code.
    child.stdin.on("error", () => { /* child exited early; `close` reports the cause */ });
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

// Percent-decoding the project/repository segments matches the CLI's own git-remote
// parser (`detectGitRemoteInfo`, ado-cli.js:69802). It was cosmetic in the source, whose
// values never left this process; it is load-bearing now that resolveConfig's values are
// forwarded into the child's AZURE_DEVOPS_PROJECT. A remote copied from the browser
// encodes spaces, so "Contoso Project" arrives as "Contoso%20Project"; left undecoded it
// is re-encoded by the API client to "Contoso%2520Project" and every call 404s.
// Guarded: decodeURIComponent throws URIError on a stray '%'.
function decodeSegment(value) {
  try { return decodeURIComponent(value); } catch { return value; }
}

function parseGitRemote(url) {
  let m;
  // https://dev.azure.com/{org}/{project}/_git/{repo}
  m = url.match(/https?:\/\/dev\.azure\.com\/([^/]+)\/([^/]+)\/_git\/(.+?)(?:\.git)?$/);
  if (m) return { orgUrl: `https://dev.azure.com/${m[1]}`, project: decodeSegment(m[2]), repository: decodeSegment(m[3]) };

  // https://{org}.visualstudio.com/{project}/_git/{repo}
  m = url.match(/https?:\/\/([^.]+)\.visualstudio\.com\/([^/]+)\/_git\/(.+?)(?:\.git)?$/);
  if (m) return { orgUrl: `https://${m[1]}.visualstudio.com`, project: decodeSegment(m[2]), repository: decodeSegment(m[3]) };

  // SSH variants
  m = url.match(/git@ssh\.dev\.azure\.com:v3\/([^/]+)\/([^/]+)\/(.+?)$/);
  if (m) return { orgUrl: `https://dev.azure.com/${m[1]}`, project: decodeSegment(m[2]), repository: decodeSegment(m[3]) };

  m = url.match(/[^@]+@vs-ssh\.visualstudio\.com:v3\/([^/]+)\/([^/]+)\/(.+?)$/);
  if (m) return { orgUrl: `https://dev.azure.com/${m[1]}`, project: decodeSegment(m[2]), repository: decodeSegment(m[3]) };

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

// `getCurrentSprint` returns the raw iteration on success. When the team has no
// `$timeframe=current` iteration the tool's rawData is `null`, and `--structured`
// falls through the `??` chain to the text envelope `{content:[…]}` with exit 0.
// So a truthy response proves nothing — presence must be tested on `.path`.
function asIteration(value) {
  return value && typeof value === "object" && typeof value.path === "string" && value.path ? value : null;
}

async function fetchCurrentIteration(teamParams, config) {
  try {
    return asIteration(await callAdoCli("getCurrentSprint", teamParams, config));
  } catch (err) {
    rethrowIfAuthInvariant(err);
    // A non-zero exit here (bad team, config, auth) must not abort the whole
    // resolution chain — the source wrapped every attempt in try/catch too.
    console.error(`[API] getCurrentSprint failed: ${err.message}`);
    return null;
  }
}

async function fetchLatestIteration(teamParams, config) {
  try {
    const all = await callAdoCli("getSprints", teamParams, config);
    const list = Array.isArray(all) ? all : [];
    return list.length > 0 ? asIteration(list[list.length - 1]) : null;
  } catch (err) {
    rethrowIfAuthInvariant(err);
    console.error(`[API] getSprints failed: ${err.message}`);
    return null;
  }
}

export async function getCurrentSprint(orgUrl, project, team) {
  const config = { orgUrl, project };

  // Team resolution: the CLI does NOT resolve a default team itself — it passes
  // `teamId` straight through as the TeamContext's `team` field (undefined when
  // omitted) and the Azure DevOps *server* resolves the project's default team.
  // That replaces the source's client-side team-name-candidate guessing
  // ([team] or [project, "${project} Team"]).
  const teamParams = team ? { teamId: team } : {};

  // Attempt 1: current iteration. Attempt 2 mirrors the source's second fallback —
  // no current sprint (between sprints, or iteration dates that don't bracket today)
  // means take the latest iteration from the full list rather than aborting the run.
  let sprint = await fetchCurrentIteration(teamParams, config);
  if (!sprint) sprint = await fetchLatestIteration(teamParams, config);

  if (!sprint) {
    // Last resort: discover a team explicitly and retry both lookups against it
    // (mirrors the source's list-teams-and-use-the-first-one fallback).
    try {
      const teams = await callAdoCli("getTeams", {}, config);
      const list = Array.isArray(teams) ? teams : [];
      if (list.length > 0) {
        console.error(`[API] Using discovered team: "${list[0].name}"`);
        const discovered = { teamId: list[0].id };
        sprint = await fetchCurrentIteration(discovered, config);
        if (!sprint) sprint = await fetchLatestIteration(discovered, config);
      }
    } catch (err) {
      rethrowIfAuthInvariant(err);
      /* ignore */
    }
  }

  if (!sprint) {
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
  const config = { orgUrl, project };
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

  // No `days` param. listWorkItems injects `AND [System.ChangedDate] >= @today - N`
  // (default 7, clamped to a hard maximum of 30 — a wider window is not expressible)
  // UNLESS the query text already mentions [System.ChangedDate], in which case the
  // filter is skipped entirely. Both WIQL bodies here end in
  // `ORDER BY [System.ChangedDate] DESC`, which trips that bypass, so the source's
  // unbounded IterationPath/State scoping is preserved.
  // DO NOT remove or rewrite the ORDER BY clause without re-checking this: dropping
  // it silently re-arms a 30-day cutoff that hides older assigned work items with
  // no error.
  const data = await callAdoCli("listWorkItems", { query: wiql, top: 100 }, config);
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

  // Same ORDER BY [System.ChangedDate] bypass as above — see the note on the first call.
  const fallback = await callAdoCli("listWorkItems", { query: fallbackWiql, top: 100 }, config);
  return (fallback?.workItems || []).map((wi) => wi.id);
}

// ---------------------------------------------------------------------------
// Work Items
// ---------------------------------------------------------------------------

// NOTE (applies to every getWorkItemsBatch call below): no `fields` list may be
// passed. The bundle always calls witApi.getWorkItems(ids, fields, undefined,
// WorkItemExpand.Relations), and Azure DevOps rejects `fields` combined with
// `$expand` (VS402337 "The fields parameter cannot be used with the expand
// parameter"). Sending `fields` fails the request outright. Omitting it returns
// every field plus the relations, and we select what we need client-side.

export async function fetchWorkItemChangedDate(orgUrl, project, id) {
  const batch = await callAdoCli("getWorkItemsBatch", { ids: [id] }, { orgUrl, project });
  const item = (Array.isArray(batch) ? batch : [])[0];
  return item?.fields?.["System.ChangedDate"] || new Date(0).toISOString();
}

// getWorkItemsBatch is the only catalog method that yields work item relations in
// their raw `{rel, url, attributes}` shape — exactly the contract the source
// produced and extractLinkedPrIds() parses. getWorkItemById is deliberately NOT
// used: it reshapes relations into `{relationshipType, artifactUri, …}` and drops
// raw dotted field keys, so it can satisfy neither consumer.
export async function fetchWorkItemDetails(orgUrl, project, id) {
  const batch = await callAdoCli("getWorkItemsBatch", { ids: [id] }, { orgUrl, project });
  const item = (Array.isArray(batch) ? batch : [])[0];
  return {
    id: item?.id ?? id,
    fields: item?.fields ?? {},
    relations: item?.relations ?? [],
  };
}

export async function fetchWorkItemComments(orgUrl, project, id) {
  const data = await callAdoCli("getWorkItemComments", { id, order: "asc", top: 200 }, { orgUrl, project });
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
  const pr = await callAdoCli("getPullRequest", { repository, pullRequestId: prId }, { orgUrl, project });

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
  } catch (err) {
    rethrowIfAuthInvariant(err);
    return false;
  }
}

export async function fetchUnresolvedThreads(orgUrl, project, repository, prId) {
  const data = await callAdoCli("getPullRequestComments", { repository, pullRequestId: prId }, { orgUrl, project });
  const threads = data?.threads || [];
  const result = [];

  for (const thread of threads) {
    if (thread.status !== "active" && thread.status !== "pending") continue;  // Only active or pending
    // Thread-level classification replaces the source's two per-thread property probes
    // (properties.CodeReviewAutoClosedByPushId / CodeReviewVoteUpdatedByIdentity):
    // the CLI's curated output does not expose thread.properties at all, but its own
    // classifyThread() maps CodeReviewThreadType "VoteUpdate"/"RefUpdate"/"StatusUpdate"/…
    // and any thread with no human-authored comment to "system", which is a superset of
    // the vote-update case. Auto-closed-by-push threads carry status closed/fixed and are
    // already excluded by the status filter above.
    if (thread.classification === "system") continue;

    // UNAVOIDABLE FIDELITY LOSS: the source additionally filtered comments to
    // `commentType === 1` (text only), dropping codeChange/system comments. The CLI
    // maps each comment to {id, author, content, publishedDate, isReply, likesCount}
    // and does not surface commentType, so that filter cannot be reproduced here —
    // thread-level classification/status above is the only available replacement.
    // Residual effect: on a *mixed* thread (human text plus non-text entries) the
    // non-text entries now appear in comments[], and a thread whose only content was
    // non-text is no longer skipped by the comments.length check. All-system threads
    // are still dropped, by classifyThread rather than by the per-comment filter.
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
    }, { orgUrl, project });
  } catch (err) {
    rethrowIfAuthInvariant(err);
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
    const timeline = await callAdoCli("getBuildTimeline", { buildId }, { orgUrl, project });

    const failedRecords = (timeline?.records || []).filter(
      (r) => r.result === "failed" && r.log?.id
    );
    if (failedRecords.length === 0) return null;

    const logId = failedRecords[0].log.id;

    // Get log metadata to know line count
    const logs = await callAdoCli("getBuildLog", { buildId }, { orgUrl, project });
    const logList = Array.isArray(logs) ? logs : [];
    const logEntry = logList.find((l) => l.id === logId);
    const lineCount = logEntry?.lineCount || 200;

    // Fetch last 150 lines
    const startLine = Math.max(1, lineCount - 150);
    const logContent = await callAdoCli("getBuildLog", {
      buildId, logId, startLine, endLine: lineCount,
    }, { orgUrl, project });

    const lines = logContent?.lines || [];
    return lines.length > 0 ? lines.join("\n") : null;
  } catch (err) {
    rethrowIfAuthInvariant(err);
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
