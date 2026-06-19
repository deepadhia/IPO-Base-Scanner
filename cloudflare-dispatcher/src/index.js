/**
 * GitHub Actions Workflow Dispatcher
 * ------------------------------------
 * Runs on Cloudflare Workers (free tier) every 15 minutes during Indian market
 * hours (UTC 03:00–11:00, Mon–Fri). Dispatches the correct GitHub Actions
 * workflow(s) for each scheduled time slot via workflow_dispatch API.
 *
 * This bypasses GitHub's internal scheduler, which is known to be delayed
 * by 30 min–3+ hours under load, making it unsuitable for intraday scanning.
 *
 * Environment variables (set as Cloudflare Worker Secrets — never in code):
 *   GITHUB_PAT   — Personal Access Token with `workflow` scope
 *
 * Cron trigger (defined in wrangler.toml):
 *   "* /15 3-10 * * 1-6"  — every 15 min, 03:00-10:59 UTC, Mon-Sat
 *   (Note: space in cron expression above is only for safe display inside JS comments)
 *
 * Schedule fired → Worker checks UTC hour:minute → dispatches workflow(s)
 */

// ─── Configuration ────────────────────────────────────────────────────────────

const GITHUB_OWNER = "deepadhia";
const GITHUB_REPO  = "IPO-Base-Scanner";
const GITHUB_REF   = "main";

const GITHUB_DISPATCH_URL = (workflow) =>
  `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${workflow}/dispatches`;

/**
 * Dispatch schedule map.
 * Key:   "HH:MM" in UTC
 * Value: array of workflow filenames to dispatch at that time (Mon–Sat only)
 *
 * Mirrors the cron expressions in .github/workflows/*.yml exactly:
 *
 *   listing-day-breakout.yml  +  watchlist-hourly-scanner.yml:
 *     '45 3-9  * * 1-5'  → :45 each hour 03–09 UTC
 *     '15 4-9  * * 1-5'  → :15 each hour 04–09 UTC
 *     '45 10   * * 1-5'  → 10:45 UTC
 *
 *   ipo-scanner-v2.yml:
 *     '45 08   * * 1-5'  → 08:45 UTC (2:15 PM IST)
 */
const SCHEDULE = {
  // ── 9:15 AM IST — market open ─────────────────────────────────────────────
  "03:45": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  // ── 9:45 AM IST ───────────────────────────────────────────────────────────
  "04:15": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  "04:45": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  // ── 10:45 AM IST ──────────────────────────────────────────────────────────
  "05:15": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  "05:45": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  // ── 11:45 AM IST ──────────────────────────────────────────────────────────
  "06:15": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  "06:45": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  // ── 12:45 PM IST ──────────────────────────────────────────────────────────
  "07:15": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  "07:45": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  // ── 1:45 PM IST ───────────────────────────────────────────────────────────
  "08:15": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  // ── 2:15 PM IST — IPO Scanner v2 daily scan ───────────────────────────────
  "08:45": [
    "listing-day-breakout.yml",
    "watchlist-hourly-scanner.yml",
    "ipo-scanner-v2.yml",        // daily consolidation scan
  ],
  // ── 2:45 PM IST ───────────────────────────────────────────────────────────
  "09:15": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  // ── 3:15 PM IST ───────────────────────────────────────────────────────────
  "09:45": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
  // ── 4:15 PM IST — post-close final scan ──────────────────────────────────
  "10:45": ["listing-day-breakout.yml", "watchlist-hourly-scanner.yml"],
};

// ─── Workflow dispatch inputs ──────────────────────────────────────────────────
// Per-workflow default inputs for workflow_dispatch.
// Extend this if you ever need to pass different inputs at specific times.
const WORKFLOW_INPUTS = {
  "ipo-scanner-v2.yml":           { mode: "scan" },
  "listing-day-breakout.yml":     {},
  "watchlist-hourly-scanner.yml": {},
};

// ─── Core dispatch function ────────────────────────────────────────────────────

async function dispatchWorkflow(workflow, pat) {
  const url     = GITHUB_DISPATCH_URL(workflow);
  const inputs  = WORKFLOW_INPUTS[workflow] ?? {};
  const body    = JSON.stringify({ ref: GITHUB_REF, inputs });

  const response = await fetch(url, {
    method:  "POST",
    headers: {
      "Authorization":        `Bearer ${pat}`,
      "Accept":               "application/vnd.github+json",
      "Content-Type":         "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent":           "cloudflare-worker-dispatcher/1.0",
    },
    body,
  });

  // 204 No Content = success for workflow_dispatch
  if (response.status === 204) {
    return { workflow, success: true, status: 204 };
  }

  // Capture error body for logging
  const errorText = await response.text().catch(() => "(no body)");
  return {
    workflow,
    success: false,
    status:  response.status,
    error:   errorText,
  };
}

// ─── Scheduled handler ─────────────────────────────────────────────────────────

export default {
  /**
   * Called by Cloudflare on each cron tick.
   * event.scheduledTime is a Unix timestamp (ms) of the exact trigger time.
   */
  async scheduled(event, env, ctx) {
    const triggerTime = new Date(event.scheduledTime);

    const utcDay    = triggerTime.getUTCDay();          // 0=Sun … 6=Sat
    const utcHour   = triggerTime.getUTCHours();
    const utcMinute = triggerTime.getUTCMinutes();
    const timeKey   = `${String(utcHour).padStart(2, "0")}:${String(utcMinute).padStart(2, "0")}`;

    console.log(`[Dispatcher] Tick at ${triggerTime.toISOString()} | day=${utcDay} key=${timeKey}`);

    // Guard: weekdays only (Mon=1 … Sat=6)
    if (utcDay === 0) {
      console.log("[Dispatcher] Sunday — no dispatch.");
      return;
    }

    const workflows = SCHEDULE[timeKey];
    if (!workflows || workflows.length === 0) {
      console.log(`[Dispatcher] No workflows scheduled at ${timeKey} UTC — no-op.`);
      return;
    }

    const pat = env.GITHUB_PAT;
    if (!pat) {
      console.error("[Dispatcher] ERROR: GITHUB_PAT secret is not configured!");
      return;
    }

    console.log(`[Dispatcher] Dispatching ${workflows.length} workflow(s) at ${timeKey} UTC:`, workflows);

    // Dispatch all workflows for this time slot in parallel
    const results = await Promise.allSettled(
      workflows.map((wf) => dispatchWorkflow(wf, pat))
    );

    // Log outcomes
    for (const result of results) {
      if (result.status === "fulfilled") {
        const { workflow, success, status, error } = result.value;
        if (success) {
          console.log(`[Dispatcher] ✅ ${workflow} dispatched (HTTP ${status})`);
        } else {
          console.error(`[Dispatcher] ❌ ${workflow} failed (HTTP ${status}): ${error}`);
        }
      } else {
        console.error(`[Dispatcher] ❌ Unexpected rejection:`, result.reason);
      }
    }
  },

  /**
   * HTTP fetch handler — handles manual workflow triggers and health-check queries.
   */
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Manual workflow trigger endpoint
    if (url.pathname === "/trigger") {
      const workflow = url.searchParams.get("workflow");
      if (!workflow) {
        return new Response(
          JSON.stringify({ error: "Missing 'workflow' query parameter" }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        );
      }

      const pat = env.GITHUB_PAT;
      if (!pat) {
        return new Response(
          JSON.stringify({ error: "GITHUB_PAT secret is not configured on Cloudflare" }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }

      console.log(`[Dispatcher] Manual HTTP trigger received for: ${workflow}`);
      const result = await dispatchWorkflow(workflow, pat);

      if (result.success) {
        return new Response(
          JSON.stringify({
            message: `Successfully dispatched workflow: ${workflow}`,
            status: result.status,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      } else {
        return new Response(
          JSON.stringify({
            error: `Failed to dispatch workflow: ${workflow}`,
            status: result.status,
            details: result.error,
          }),
          { status: result.status || 500, headers: { "Content-Type": "application/json" } }
        );
      }
    }

    // Default: Health check / Status page
    const now = new Date().toISOString();
    return new Response(
      JSON.stringify({
        service:  "IPO-Base-Scanner Workflow Dispatcher",
        status:   "healthy",
        time_utc: now,
        schedule_slots: Object.keys(SCHEDULE).length,
        endpoints: {
          health: "/",
          trigger: "/trigger?workflow=<workflow_filename>"
        }
      }),
      {
        headers: { "Content-Type": "application/json" },
      }
    );
  },
};
