"use client";

import { useState } from "react";
import { createTask } from "@/lib/api";

export default function HomePage() {
  const [repoUrl, setRepoUrl] = useState("https://github.com/akashpersetti/repomodernizer-demo-target");
  const [goal, setGoal] = useState("Migrate this Flask app to FastAPI with async route handlers.");
  const [testCommand, setTestCommand] = useState("pytest -q");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const { task_id } = await createTask({ repo_url: repoUrl, goal, test_command: testCommand });
      window.location.href = `/task?id=${task_id}`;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  }

  return (
    <main className="max-w-xl mx-auto py-16 px-4">
      <h1 className="text-2xl font-semibold mb-2">RepoModernizer</h1>
      <p className="text-gray-600 mb-6 text-sm">
        Autonomous repository modernization agent — durable, human-gated, deployed on AWS.
      </p>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium mb-1">Repo URL</label>
          <input
            className="w-full border rounded px-3 py-2"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Goal</label>
          <input
            className="w-full border rounded px-3 py-2"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Test command</label>
          <input
            className="w-full border rounded px-3 py-2"
            value={testCommand}
            onChange={(e) => setTestCommand(e.target.value)}
            required
          />
        </div>
        {error && <p className="text-red-600 text-sm">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="bg-black text-white px-4 py-2 rounded disabled:opacity-50"
        >
          {submitting ? "Starting..." : "Start migration"}
        </button>
      </form>
    </main>
  );
}
