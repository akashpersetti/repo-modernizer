"use client";

import { useEffect, useState } from "react";

export default function ArchitectureModal() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded border px-3 py-1.5 text-sm hover:bg-gray-50"
      >
        View architecture
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          onClick={() => setOpen(false)}
        >
          <div
            className="max-h-[90vh] w-full max-w-4xl overflow-auto rounded-lg bg-white p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-sm font-medium text-gray-700">Architecture</h3>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="rounded px-2 py-1 text-gray-500 hover:bg-gray-100"
              >
                ✕
              </button>
            </div>
            <img
              src="/architecture.svg"
              alt="Low-level architecture: browser hits CloudFront/S3 for the static UI and API Gateway for fetches; API Gateway routes to a Lambda running FastAPI, which enqueues onto SQS and reads state directly from DynamoDB; SQS triggers a consumer Lambda that runs a one-shot Fargate task per message; the Fargate worker checkpoints every step to DynamoDB, uses EFS as its git workspace, and clones/commits/pushes/opens a PR against GitHub."
              className="mx-auto w-full max-w-2xl"
            />
          </div>
        </div>
      )}
    </>
  );
}
