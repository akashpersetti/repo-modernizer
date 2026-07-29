"use client";

import { useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { approveTask, getTaskStatus, TaskStatus } from "@/lib/api";

function TaskView() {
  const params = useSearchParams();
  const taskId = params.get("id") ?? "";
  const [status, setStatus] = useState<TaskStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  useEffect(() => {
    if (!taskId) return;
    let cancelled = false;
    async function poll() {
      try {
        const s = await getTaskStatus(taskId);
        if (!cancelled) setStatus(s);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }
    poll();
    const interval = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [taskId]);

  async function handleDecision(decision: "approve" | "reject") {
    if (!status?.awaiting_approval) return;
    setActing(true);
    try {
      await approveTask(taskId, status.awaiting_approval.path, decision);
    } finally {
      setActing(false);
    }
  }

  if (!taskId) return <p className="p-8">No task id in URL.</p>;
  if (error) return <p className="p-8 text-red-600">{error}</p>;
  if (!status) return <p className="p-8">Loading...</p>;

  return (
    <main className="max-w-3xl mx-auto py-12 px-4 space-y-6">
      <h1 className="text-xl font-semibold">Task {taskId}</h1>

      {status.awaiting_approval && (
        <div className="border border-yellow-400 bg-yellow-50 rounded p-4 space-y-3">
          <p className="font-medium">
            Awaiting approval — {status.awaiting_approval.path} (risk {status.awaiting_approval.risk_score.toFixed(2)})
          </p>
          <pre className="bg-black text-green-400 text-xs p-3 rounded overflow-x-auto">
            {status.awaiting_approval.diff}
          </pre>
          <div className="space-x-2">
            <button
              onClick={() => handleDecision("approve")}
              disabled={acting}
              className="bg-green-600 text-white px-3 py-1.5 rounded disabled:opacity-50"
            >
              Approve
            </button>
            <button
              onClick={() => handleDecision("reject")}
              disabled={acting}
              className="bg-red-600 text-white px-3 py-1.5 rounded disabled:opacity-50"
            >
              Reject
            </button>
          </div>
        </div>
      )}

      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="text-left border-b">
            <th className="py-2">File</th>
            <th>Status</th>
            <th>Tokens</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {Object.values(status.files).map((f) => (
            <tr key={f.path} className="border-b">
              <td className="py-2">{f.path}</td>
              <td>{f.status}</td>
              <td>{f.tokens}</td>
              <td>${f.cost_usd.toFixed(4)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="text-sm text-gray-600">Total cost: ${status.cost_used_usd.toFixed(4)}</p>

      {status.done && (
        <div className="border border-green-400 bg-green-50 rounded p-4">
          <p className="font-medium">Done.</p>
          {status.pr_url ? (
            <a href={status.pr_url} target="_blank" rel="noreferrer" className="text-blue-600 underline">
              View pull request
            </a>
          ) : (
            <p className="text-sm text-gray-600">No files were migrated — no PR opened.</p>
          )}
        </div>
      )}
    </main>
  );
}

export default function TaskPage() {
  return (
    <Suspense fallback={<p className="p-8">Loading...</p>}>
      <TaskView />
    </Suspense>
  );
}
