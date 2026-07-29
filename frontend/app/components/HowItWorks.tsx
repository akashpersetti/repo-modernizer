export default function HowItWorks() {
  return (
    <section className="rounded border bg-white px-6 py-10 text-center sm:px-10">
      <h2 className="text-balance text-2xl font-semibold sm:text-3xl">
        Give it a repo and a goal. It migrates the code.
      </h2>
      <p className="mx-auto mt-3 max-w-xl text-balance text-gray-600">
        You point it at a repo and describe the goal in plain English. Behind the scenes, an
        agent plans the migration file by file, rewrites and tests each one, pausing for your
        approval on risky diffs, then opens a pull request with the result.
      </p>

      <img
        src="/how-it-works.svg"
        alt="Four steps: you give it a repo and a goal, the agent plans the migration file by file, the agent rewrites and tests each file and pauses on risk, you get an opened pull request."
        className="mx-auto mt-8 w-full max-w-2xl"
      />
    </section>
  );
}
