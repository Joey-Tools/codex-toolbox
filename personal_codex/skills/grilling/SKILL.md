---
name: grilling
description: Stress-test a plan, decision, or idea through a rigorous design-tree interview with adaptive proposal or alternatives rounds. Use only when the user explicitly invokes `$grilling`.
---

# Grilling

Interview the user until you reach a shared understanding. Map the discussion as
a **design tree**: every decision branches into the decisions that depend on it.

## Invocation Gate

- Run this skill only after the user explicitly invokes `$grilling`.
- Do not infer or implicitly trigger a grilling session from ordinary planning,
  decision-making, critique, or stress-testing language.

## Build the Design Tree

1. Identify unsettled decisions and the prerequisites for each one.
2. Define the **frontier** as every unsettled decision whose prerequisites are
   settled. Ask only about decisions on the current frontier.
3. Resolve accessible facts before asking the user to decide. Search the
   filesystem, tools, documentation, or another available source directly.
   Dispatch a sub-agent when independent exploration is useful.
4. Treat a running investigation as an unsettled prerequisite. Postpone only
   its dependent nodes and continue with other ready frontier nodes.
5. After every answer round, recompute the entire tree and frontier. Answers can
   remove, reshape, or unblock decisions; never continue a stale queue.

Facts are the agent's job. If the user challenges a premise or asks a factual
question, investigate it and return to the same decision. Do not count that
exchange as a decision answer.

## Route Each Frontier Decision

Choose a presentation independently for each frontier node:

- Use **alternatives mode** when two or more viable, non-dominated directions
  remain and their ordering depends on an unsettled preference.
- Default to alternatives mode for protected decisions: scope or execution
  authorization, destructive or hard-to-reverse actions, external publication
  or spending, permissions or credentials, trust boundaries, security,
  privacy, compliance, public APIs, compatibility commitments, persistent
  schemas, migrations, or choices that require external coordination to undo.
- Automatically use **proposal mode** only when settled facts, constraints,
  priorities, and prior answers leave one non-dominated direction, and the
  decision is local or reasonably reversible.
- When the route is uncertain, use alternatives mode.

Honor an explicit presentation request for the node, round, or session scope
the user names when that form can represent the decision honestly. The override
changes presentation only: it does not fabricate dominance, turn an assumption
into a settled priority, or relax the fact, context, confirmation, or
authorization invariants. A newer, more specific preference wins. In
particular, **Reject / compare alternatives** creates a node-scoped alternatives
override for one complete comparison and takes precedence over an older round-
or session-scoped proposal preference. Do not infer a future preference for
proposal mode merely because the user repeatedly adopts recommendations.

## Assemble a Round

- Never combine dependent decisions in one round.
- A proposal round contains exactly one frontier node.
- Alternatives mode has no fixed question count per round and no fixed option
  count per question. Include every materially distinct viable alternative
  that has not been eliminated by settled constraints. Never invent an option
  to fill a quota or omit a real fourth or fifth direction to fit a UI.
- Size an alternatives round from the frontier width, shared context, total
  reading load, decision difficulty, and consequence weight. A round is small
  enough when the user can read it once and answer without rereading it or
  juggling unrelated mental models.
- Split the round when a root or high-impact decision deserves focused
  attention, context or options are long, questions use different decision
  criteria, the combined output becomes tiring, or one answer could change
  another question. Several light independent sibling nodes may share a round.

## Present Proposal Mode

State the proposal as an affirmative recommendation whose polarity matches the
question. Give enough context for the user to audit it without reconstructing
the analysis. Include:

1. **Problem and timing**: the concrete issue and why it is ready now.
2. **Relevant context**: facts, constraints, prior decisions, and clearly
   labelled assumptions.
3. **Proposal**: one specific affirmative recommendation.
4. **Why it leads**: why it dominates under the settled priorities. Under an
   explicit presentation override, disclose when it does not dominate and
   identify the assumption or missing preference behind the recommendation.
5. **Tradeoffs**: gains, costs, risks, and important non-guarantees.
6. **Strongest alternative**: the most credible competing direction and why it
   is not preferred. If no credible live alternative remains, say so and name
   the nearest eliminated candidate plus the constraint that rules it out.
7. **Impact and next action**: what adopting the proposal changes.
8. **Reopen condition**: new evidence or a changed constraint that would
   invalidate the recommendation.

Then request one explicit disposition:

- **Adopt (Recommended)**: accept the proposal exactly as stated. Restating an
  existing condition is still adoption; adding or changing a condition is a
  revision.
- **Revise**: keep the direction open and change named parameters.
- **Reject / compare alternatives**: do not accept it and reopen the same node
  in alternatives mode.

Proposal mode is not a short-answer mode. Do not replace the required context
with a bare recommendation and approval prompt.

## Present Alternatives Mode

For each question, provide:

1. The specific decision, why it is on the frontier now, and the relevant
   facts, constraints, prior decisions, and labelled assumptions.
2. Every materially distinct viable option, each with its practical effect,
   main upside, main cost, and important risk or non-guarantee.
3. A recommendation after the fair comparison when one option honestly leads,
   with the settled priority that makes it preferable. If no option leads
   without a missing preference, state that missing preference instead of
   fabricating a recommendation.
4. An invitation to select an option, modify one, combine genuinely compatible
   choices, or answer freely.

Do not force composable choices into a false mutually exclusive menu. Split
them into independent decisions or collect the combination in text.

## Choose the Interaction Surface

Use `request_user_input` when it is available and the natural round fits its
interface: one to three questions, each with two or three mutually exclusive
choices, with an honest recommended choice for every question. First present
the complete proposal brief or alternatives comparison in the user-visible
conversation; then use the picker only to collect the disposition or selection.
Put the recommended choice first and suffix its label with `(Recommended)`.
Do not add an `Other` option when the client supplies the free-form path.

If the real round has more questions, more options, compatible selections, or a
question with no honest recommendation, use structured text even when the tool
is available. Number questions and options, keep the same semantics, state an
honest recommendation or the missing preference after each comparison, and
explicitly invite a free-form answer. The picker is a transport convenience,
not a container for the full reasoning and not a reason to truncate the
decision space.

If the tool is unavailable, fails, or returns an empty `answers` object, ask
the same round in text and wait for explicit input. Do not change modes merely
because the collaboration mode or interaction surface changed.

Never set or describe a timeout, countdown, auto-submit behavior, or
silence-based default. Silence, timeout, and an empty answer are unanswered;
never treat them as adoption.

## Apply Answers

- Settle only the decisions the user explicitly answered. Preserve free-form
  qualifications as constraints or revisions rather than rounding them to the
  nearest preset choice.
- If a round receives only some answers, apply every non-empty answer first,
  recompute the entire tree, and re-present only unanswered nodes that remain
  reachable and valid. Never replay a now-stale question.
- On **Revise**, keep the proposal node open, apply the supplied changes, and
  recompute the entire tree before asking anything else. If the node remains
  reachable and needs more input, ask only for the minimum missing parameters
  needed to reformulate it. A revision is not partial adoption.
- Treat a newly added or changed condition on a proposal as **Revise**, not
  **Adopt**. A condition already stated in the proposal may be acknowledged
  without changing exact adoption.
- On **Reject / compare alternatives**, do not reword and push the same
  proposal. Reopen that node in alternatives mode unless the user supplied a
  replacement direction directly.
- If new evidence invalidates a displayed but unanswered question, withdraw it
  explicitly and reroute it. Otherwise do not silently change the presentation
  of an unanswered node.
- A design answer settles a design branch only. It does not authorize unrelated
  external actions, scope expansion, publication, spending, deletion, or other
  mutations that require separate authority.

Read [references/interaction-modes.md](references/interaction-modes.md) when a
route is ambiguous, a round exceeds picker limits, the user changes modes, or
you are testing or revising this skill. It contains the full routing table,
schemas, state transitions, boundary cases, and behavioral test matrix.

## Finish Only After Confirmation

Finish only when every reachable node is settled or explicitly pruned and there
is no pending investigation, `blocked-on-fact` node, or other reachable
unsettled node. An empty ready frontier alone is not completion. Summarize the
shared understanding, including accepted tradeoffs and any explicit
assumptions, then request one final disposition: **Confirm and proceed
(Recommended)**, **Revise**, or **Pause**.

Use `request_user_input` for that final choice when available; otherwise use
text. Do not act on the resulting plan, decision, or idea until the user
explicitly confirms the shared understanding.
