const API_URL = process.env.NEXT_PUBLIC_API_URL || "https://6yncgq73gk.execute-api.us-east-1.amazonaws.com";

export type FileStatus = {
  path: string;
  status: string;
  tokens: number;
  cost_usd: number;
  retry_count: number;
  last_error: string | null;
};

export type TaskStatus = {
  task_id: string;
  files: Record<string, FileStatus>;
  cost_used_usd: number;
  awaiting_approval: { path: string; diff: string; risk_score: number } | null;
  done: boolean;
  pr_url: string | null;
};

export async function createTask(input: {
  repo_url: string;
  goal: string;
  test_command: string;
  base_branch?: string;
}): Promise<{ task_id: string }> {
  const res = await fetch(`${API_URL}/tasks`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(`create task failed: ${res.status} ${await res.text()}`);
  return res.json();
}

export async function getTaskStatus(taskId: string): Promise<TaskStatus> {
  const res = await fetch(`${API_URL}/tasks/${taskId}`);
  if (!res.ok) throw new Error(`get status failed: ${res.status} ${await res.text()}`);
  return res.json();
}

export async function approveTask(
  taskId: string,
  file: string,
  decision: "approve" | "reject",
  note = ""
): Promise<void> {
  const res = await fetch(`${API_URL}/tasks/${taskId}/approve`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ file, decision, note }),
  });
  if (!res.ok) throw new Error(`approve failed: ${res.status} ${await res.text()}`);
}
