---
name: grilling
description: Stress-test a plan, decision, or idea through a rigorous design-tree interview. Use only when the user explicitly invokes `$grilling`.
---

# Grilling

Interview the user relentlessly until you reach a shared understanding. Map the
discussion as a **design tree**: every decision branches into the decisions that
depend on it.

## Invocation Gate

- Run this skill only after the user explicitly invokes `$grilling`.
- Do not infer or implicitly trigger a grilling session from ordinary planning,
  decision-making, critique, or stress-testing language.

## Work the Design Tree

1. Identify the unsettled decisions and their prerequisites.
2. Define the **frontier** as every decision whose prerequisites are settled.
   Ask only questions that are on the current frontier.
3. Select at most three frontier questions for the next batch. If more ready
   questions remain, tell the user that another batch remains and, when known,
   how many ready questions are still waiting.
4. After every answer batch, recompute the entire tree and frontier. Do not
   mechanically continue a stale batch list; answers can remove, reshape, or
   unblock decisions.
5. Keep dependent questions for a later batch. Never ask a question whose
   answer depends on another unsettled question in the same batch.

## Ask with `request_user_input`

When the `request_user_input` tool is available, use it for every decision
batch and for the final confirmation.

- Put one to three questions in each tool call.
- Give every question exactly three mutually exclusive choices. Put the
  recommended choice first and suffix its label with `(Recommended)`.
- Make each option description state its practical impact or tradeoff.
- Do not add an `Other` option. The client supplies the free-form answer path.
- Do not set or describe a timeout, countdown, auto-submit behavior, or
  silence-based default. Wait indefinitely for the user's response.
- Do not select the recommended choice merely because the user has not replied.

When the tool is unavailable, ask the same batch in text. Number every question,
include exactly three choices plus an explicit free-form-answer invitation, and
format each question like this:

```
❓ **Q1** - **<question title>**: <question body and three choices>

➡️ <recommended answer and why>
```

Then wait for the user's answers before asking another batch.

## Resolve Facts, Escalate Decisions

Finding facts is the agent's job, never the user's. When a frontier question
needs evidence from the filesystem, tools, documentation, or another accessible
source, find it directly. Dispatch a sub-agent when independent exploration is
useful.

Do not block the entire session on a running exploration. Treat its result as an
unsettled prerequisite, postpone only the dependent questions, and ask the rest
of the current frontier. Decisions remain the user's: present each decision with
a recommendation and wait for their answer.

## Finish Only After Confirmation

The session is ready to finish only when the frontier is empty: every reachable
branch has been visited and nothing remains silently assumed. Summarize the
shared understanding, then ask the user to confirm it. When
`request_user_input` is available, use three choices: confirm and proceed,
revise the design, or pause, with confirm and proceed recommended.

Do not act on the resulting plan, decision, or idea until the user explicitly
confirms the shared understanding.
