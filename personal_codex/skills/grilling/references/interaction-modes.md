# Adaptive Interaction Modes

Use this reference when routing or rendering a `$grilling` frontier node. The
two presentation modes share the same design tree, evidence rules, and explicit
answer requirement. They differ in the decision shape they expose:

- `proposal` presents one reasoned recommendation for adoption, revision, or
  comparison.
- `alternatives` presents every materially distinct viable direction that the
  user still needs to choose among.

Mode selection is per frontier node. Do not set one mode for the entire session
unless the user explicitly requests that scope.

## Routing Truth Table

Evaluate the rows from top to bottom for each reachable candidate node while
computing the decision frontier. First mark a node that needs accessible
evidence as `blocked-on-fact`; only `ready` nodes whose prerequisites are
settled enter the frontier and continue through the presentation rows. A
presentation preference controls the interaction form; it does not turn a fact
into a user decision, settle prerequisites, or authorize execution.

| Priority | Condition | Result | Required handling |
| --- | --- | --- | --- |
| 1 | An accessible fact is required before the node can be decided | `investigate` | Find the fact directly. Suspend only dependent nodes and continue with other ready nodes. |
| 2 | The user explicitly requests `proposal` or `alternatives` for this node, round, or session | Requested mode | Apply the preference only to the scope the user named. This is a presentation-only override: preserve every shared invariant and do not fabricate options or dominance. A newer, more specific node preference takes precedence. |
| 3 | The node controls a protected, high-consequence, hard-to-reverse, or trust-sensitive choice | `alternatives` | Expose the real choice surface before asking the user to decide. Examples include security, permissions, public APIs, compatibility, migrations, destructive changes, and external authorization. |
| 4 | Two or more viable directions remain non-dominated because the answer depends on an unsettled user preference | `alternatives` | Compare all materially distinct viable directions. Do not substitute the agent's unstated preference. |
| 5 | Exactly one viable direction dominates under settled goals and constraints, and the decision is local or reasonably reversible | `proposal` | Present the dominant direction with enough context to audit it. |
| 6 | The mode remains uncertain | `alternatives` | Preserve the unresolved choice rather than hiding it in a recommendation. |
| 7 | No reachable unsettled node remains | `finish` | Summarize the shared understanding and request final confirmation. |

Treat repeated adoption of proposals as answers to those nodes, not as an
implicit request to use `proposal` for future nodes. A user can override the
automatic route at any time. If the user does not state the override's scope,
apply it to the current node only. `Reject / compare` creates a newer,
node-scoped alternatives override that outranks an older round- or session-wide
proposal preference until one complete comparison is presented or the user
explicitly switches that node again. The older session preference may still
apply to later nodes; it must not loop the rejected node back into proposal.

### Routing Definitions

- A direction is **viable** when it satisfies every settled hard constraint.
- A direction is **dominated** when another viable direction is at least as good
  on every settled priority and better on at least one, without introducing a
  new unsettled preference.
- A **user preference** is a value judgment, risk tolerance, or priority the
  agent cannot establish from accessible evidence.
- A decision is **protected or high-consequence** when a mistaken answer could
  materially alter access, data integrity, compatibility, public commitments,
  external state, or the cost of reversal.

Do not present eliminated directions as live alternatives. When the user asks
for `alternatives` but settled constraints leave only one viable direction,
state that result, identify any materially relevant eliminated directions and
why they fail, and ask whether the user wants to relax a constraint. Never
invent a second or third option to fill a format.

## Proposal Schema

A proposal round handles exactly one frontier node. It is a decision brief, not
a shortened alternatives question. Include all of these fields in a natural
order:

1. **Decision and why now**: name the specific unresolved problem and why it is
   on the frontier.
2. **Relevant context**: state the facts, settled constraints, and prior
   decisions that govern the recommendation. Label assumptions separately.
3. **Recommendation**: make one affirmative, concrete proposal.
4. **Why it leads**: connect the proposal to the user's settled priorities and
   explain why it currently leads. If an explicit presentation override is the
   only reason proposal mode is in use, say that the direction does not
   dominate, identify the missing preference or labelled assumption behind the
   recommendation, and do not manufacture certainty.
5. **Tradeoffs**: state gains, costs, risks, and important non-guarantees. Do not
   hide a real downside in the recommendation rationale.
6. **Strongest credible alternative**: name it and explain why it is not
   preferred under the current constraints. If no credible live alternative
   remains, say `No credible live alternative`, then identify the nearest
   eliminated candidate and the settled constraint that eliminates it; do not
   relabel that candidate as viable.
7. **Impact**: explain what accepting the proposal would settle or unblock and
   what the next design step would be.
8. **Reopen condition**: name evidence or a constraint change that would make
   the recommendation worth revisiting.
9. **Disposition**: ask for an explicit `Adopt`, `Revise`, or
   `Reject / compare` response and welcome a free-form answer.

Keep the recommendation and question polarity aligned. For example, ask
"Should we adopt X?" and recommend "Adopt X". Do not ask whether to avoid X and
then make an affirmative X option the recommendation.

Use these disposition semantics:

- **Adopt (Recommended)** settles the proposal exactly as stated. Repeating or
  acknowledging a condition already in the proposal does not change adoption.
- A newly added or changed condition is **Revise**, not adoption. Keep the node
  open, incorporate the condition, and recompute the entire tree. If the node
  remains reachable and needs more input, obtain only the minimum missing
  parameter before presenting the revised proposal for an explicit disposition.
- **Revise** keeps the node open. Apply the supplied changes and recompute the
  entire tree first. Only if the node remains reachable and still needs input,
  ask for the minimum parameters needed to revise the proposal.
- **Reject / compare** keeps the same node open and presents it in
  `alternatives` mode next. Do not rephrase and push the rejected proposal
  again.

If a free-form response unambiguously adopts, revises, rejects, or selects a
different direction, honor the substance rather than requiring the disposition
label. Ask one narrow follow-up only when the response leaves the decision
ambiguous.

### Proposal Text Shape

Use headings or compact labels so the reasoning can be audited in one pass:

```text
Decision: <specific frontier node and why it matters now>

Context
- Facts: <verified facts>
- Constraints and prior decisions: <settled inputs>
- Assumptions: <remaining assumptions, or "None">

Proposal
<affirmative recommendation>

Why this leads
<reasoning tied to settled priorities>

Tradeoffs
- Gains: <benefits>
- Costs and risks: <costs, risks, and non-guarantees>
- Strongest alternative: <alternative and why it is not preferred>

Impact and reopen condition
<what this settles or unblocks; what new evidence would reopen it>

Choose: Adopt (Recommended), Revise, or Reject / compare. A free-form answer is welcome.
```

## Alternatives Schema

An alternatives question exposes a real choice surface. Include:

1. **Decision and why now**: identify the frontier node and its consequence.
2. **Shared context**: give the facts, settled constraints, prior decisions, and
   separately labeled assumptions needed by every option.
3. **Complete viable set**: include every materially distinct, viable direction
   that has not been eliminated by settled constraints. There is no fixed
   minimum or maximum option count.
4. **Per-option consequences**: explain the practical upside, downside, risk,
   and relevant reversibility of each direction.
5. **Fair comparison**: compare options using the same decision criteria before
   stating the recommendation.
6. **Recommendation**: identify the leading option and explain which settled
   priorities make it lead. If no option leads without a missing preference,
   say so and identify that preference instead of inventing certainty.
7. **Explicit answer path**: ask the user to choose, combine where genuinely
   valid, reject the set, or answer in free form.

Do not force false mutual exclusivity. When independently compatible choices
are separate decisions, model them as separate frontier nodes. When a combined
direction is itself a meaningful one-of-many strategy, include that combination
as a real option. Do not create arbitrary pairwise tournaments or generic
"other" buckets to fit a UI limit.

### Alternatives Text Shape

Use a numbered question and lettered alternatives. Repeat the structure for
each node in the selected round:

```text
Q1 — <decision and why it matters now>

Context
- Facts and constraints: <shared verified inputs>
- Assumptions: <remaining assumptions, or "None">

Alternatives
A. <direction>: <upside, downside, risk, and reversibility>
B. <direction>: <upside, downside, risk, and reversibility>
...

Comparison: <fair comparison on common criteria>
Recommendation: <leading direction and why, or the missing preference>

Answer with Q1: <choice or free-form decision>.
```

For a multi-question round, give each question enough local context to stand on
its own without duplicating a long shared preamble. Tell the user how to map
answers to question numbers.

## Size An Alternatives Round

Choose the round before choosing the rendering tool. There is no fixed question
count. Size the round from all of these signals:

- **Frontier width**: how many independent nodes are ready now.
- **Reading load**: total context, option descriptions, and comparison text the
  user must absorb.
- **Decision difficulty**: how much judgment or unfamiliar reasoning each node
  requires.
- **Consequence weight**: how costly a mistaken or premature answer would be.
- **Context overlap**: whether the nodes use the same facts and decision
  criteria.

A round is acceptably sized when the user can read it once and answer every
question without rereading earlier sections or constructing several unrelated
mental models. This is the operative limit, not a numeric cap.

Split the round when any of these conditions holds:

- A root or high-consequence decision deserves focused attention.
- A question has long context, many alternatives, or difficult tradeoffs.
- Questions rely on materially different facts or decision criteria.
- The combined output would be tiring to read or easy to answer incorrectly.
- One answer could change, remove, or reshape another question. Such questions
  are dependent and must never share a round.

It is valid to ask more than three light, independent sibling questions in one
text round when they share context and the total cognitive load remains low. It
is also valid to ask only one question when the frontier is wide but that
question is difficult or consequential. Do not announce a fixed number of
questions remaining: the tree can change after every answer.

After each answer round, recompute the complete design tree and frontier. Never
continue a prewritten queue merely because those questions were previously
ready.

## Choose Tool Or Text Rendering

`request_user_input` is a rendering convenience, not the semantic contract.
Use it only when the already-selected natural round fits its shape:

- one to three questions; and
- two or three real answer options for every question; and
- one option honestly leads for every question, so the required recommended
  label does not fabricate a preference.

Rendering is two-layered. First present the complete proposal brief or
alternatives comparison in the user-visible conversation, including all
context, tradeoffs, risks, and non-guarantees. Then call the picker only to
collect the disposition or selection. Its short question and option-description
fields may summarize consequences but must never replace the preceding
reasoning.

For a `proposal`, the three tool options are `Adopt (Recommended)`, `Revise`,
and `Reject / compare`, each with a one-sentence practical consequence. For an
`alternatives` question, put the recommended real direction first and mark it
`(Recommended)`. Describe the practical impact or tradeoff of every option. If
no direction honestly leads without an unsettled preference, the picker is
inapplicable and the comparison must remain in structured text. The
client-provided free-form path remains available; do not add a synthetic
`Other` option.

Use structured text even when the tool is available if the natural round has
more than three questions, any question has more than three viable
alternatives, the options are not naturally representable by the picker, or the
full reasoning would be lost in short option descriptions. Do not truncate the
real choice set, split an otherwise coherent round solely to satisfy the tool,
or manufacture grouping solely for UI compatibility.

The cognitive-load rules may independently justify splitting a round. A real
dependency or a natural hierarchy may also justify separate rounds; the picker
limit alone may not.

Never set a timeout, countdown, auto-submit behavior, or silence-based default.
An empty tool result or silence leaves every affected node unanswered. For a
partial tool or text response, apply all non-empty answers, recompute the entire
tree, and then re-present only still-reachable, still-valid unanswered nodes.
Do not replay a question invalidated by an answered sibling, and do not silently
choose the recommendation.

## State Transitions

Track node state semantically; labels in the conversation do not need to expose
this implementation vocabulary.

| Current state | Event | Next state and action |
| --- | --- | --- |
| `blocked-on-fact` | Required accessible evidence is missing | Investigate; leave only dependent nodes blocked. |
| `blocked-on-fact` | Evidence arrives | Update facts and recompute the entire tree before presenting anything. |
| `ready` | Router selects `proposal` | Present one proposal and wait. |
| `ready` | Router selects `alternatives` | Add the node to a cognitively coherent alternatives round and wait. |
| `proposal-presented` | User adopts exactly as stated or merely repeats an existing proposal condition | Mark settled and recompute the tree. |
| `proposal-presented` | User adds or changes a condition | Treat as `Revise`; keep open, incorporate the condition, recompute first, and request any still-needed parameter only if the node remains reachable. |
| `proposal-presented` | User requests revision | Keep open, apply supplied changes, and recompute first; only then obtain missing revision parameters if the node remains reachable. |
| `proposal-presented` | User rejects or asks to compare | Keep open and transition the same node to `alternatives`. |
| `alternatives-presented` | User selects a direction | Mark settled, record explicit conditions, and recompute the tree. |
| `alternatives-presented` | User combines compatible directions | Settle only if the combination is coherent; otherwise split the independent decisions and clarify. |
| `alternatives-presented` | User rejects all options or changes a constraint | Keep open, revise the viable set, and recompute. |
| Any presented state | Answer is empty, silent, or ambiguous | Leave open; re-present or ask the minimum narrow clarification. |
| `settled` | New evidence satisfies its stated reopen condition or invalidates a governing fact | Reopen explicitly, explain why, and recompute affected branches. |
| Any state | An answer removes, reshapes, or unblocks other nodes | Recompute the full tree; do not use a stale question queue. |
| All reachable nodes are settled or pruned, with no pending investigation or `blocked-on-fact` state | Ready frontier is empty | Summarize the shared understanding and ask `Confirm and proceed`, `Revise`, or `Pause`. |
| Final confirmation | User confirms | End grilling. Perform only actions already in scope and separately authorized. |
| Final confirmation | User revises or pauses | Reopen the named node or stop without treating silence as confirmation. |

A decision answer settles design intent only. It does not authorize unrelated
external writes, messages, publication, destructive operations, or other
actions outside the user's request.

## Boundary Cases

1. **One feasible implementation remains.** Use `proposal`; say that there is no
   credible live alternative and name the nearest constraint-eliminated
   candidate. Do not invent two inferior implementations to create a picker.
2. **Two good implementations optimize different unsettled priorities.** Use
   `alternatives`, even if the agent has a personal favorite.
3. **Five viable strategies remain.** Render all five in text. Do not omit two,
   hide them under `Other`, or run an arbitrary tournament.
4. **Four light sibling choices share one short context block.** One text round
   can contain all four when each is easy and independent.
5. **Two difficult migration choices are independent.** Ask them separately if
   their combined reading or consequence load is high.
6. **Q2 depends on Q1.** Ask Q1 alone, recompute, and only then decide whether Q2
   still exists.
7. **A natural two-question, three-option round fits the picker.** If every
   question has an honest leader, present the full reasoning first and then use
   `request_user_input` only to collect the selections.
8. **The user explicitly requests a proposal for a protected decision.** Honor
   the presentation preference when one recommendation can be stated honestly,
   but disclose that the presentation-only override does not establish
   dominance. Retain the complete proposal context, strongest alternative,
   risks, non-guarantees, reopen condition, and explicit disposition. The
   preference does not authorize action.
9. **The user asks for alternatives but only one is viable.** Disclose the sole
   viable direction and the constraints eliminating expected candidates. Ask
   whether to reconsider a constraint; do not mislabel eliminated candidates as
   live options.
10. **The user adopts several proposals in a row.** Route the next node from its
    own structure unless the user explicitly requested ongoing proposal mode.
11. **The user rejects a proposal.** Open the same node in `alternatives`; do not
    repeat the proposal with softer wording.
12. **An answer contains a custom direction.** Evaluate it against settled
    constraints, add it to the tree if viable, and recompute before asking the
    next question.
13. **A filesystem or documentation lookup can answer the apparent question.**
    Investigate it instead of asking the user to choose a fact.
14. **New evidence contradicts an adopted proposal's governing assumption.**
    Reopen the node, cite the changed input, and explain the affected branches.
15. **Options can be combined independently.** Model separate decisions rather
    than pretending they are mutually exclusive; include a combined strategy
    only when it is genuinely a distinct strategy.
16. **The user does not answer.** Wait indefinitely. Never treat delay, an empty
    result, or conversation silence as adoption.
17. **An older session-wide proposal preference is active and the user rejects
    one proposal.** Apply a newer node-scoped alternatives override for one
    complete comparison. Do not route that same node straight back to proposal.
18. **Alternatives differ only by an unsettled priority.** State the missing
    preference and use structured text; do not invent a recommendation merely
    to satisfy the picker.
19. **Only some questions receive answers.** Apply every non-empty answer,
    recompute the tree, and re-present only unanswered nodes that remain valid.
20. **Every ready node is blocked on evidence.** Continue or await the pending
    investigations; an empty ready frontier is not completion.
21. **The user says "adopt, but change X."** Treat the new condition as
    `Revise`; exact adoption applies only after the revised proposal is stated
    and accepted.

## Behavioral Test Matrix

Test decisions and transitions, not exact prose or heading names.

| ID | Setup | Expected behavior |
| --- | --- | --- |
| R1 | One dominant, reversible direction under settled constraints | Routes to one-node `proposal`; includes all nine schema elements. |
| R2 | Two non-dominated directions depend on risk tolerance | Routes to `alternatives`; describes both fairly and identifies the missing preference. |
| R3 | Security or permission boundary with several viable choices | Defaults to `alternatives` and does not imply authorization. |
| R4 | Routing evidence is accessible but not yet known | Investigates instead of asking the user; unrelated frontier nodes can continue. |
| R5 | Mode cannot be confidently classified | Defaults to `alternatives`. |
| R6 | User requests proposal for only Q2 | Overrides Q2 only; other nodes continue adaptive routing. |
| R7 | User says "use proposals for the rest of this session" | Persists proposal presentation for later nodes while retaining facts, confirmation, and authorization invariants. |
| R8 | User has adopted the last three proposals but stated no preference | Does not infer a session-wide proposal preference. |
| R9 | User requests a proposal for a protected decision with no settled dominant direction | Treats the override as presentation-only, discloses missing preference or assumption, and does not fabricate dominance or authorization. |
| R10 | Session proposal preference exists, then the user rejects one proposal | Applies a newer node-scoped alternatives override for one complete comparison instead of re-pushing the proposal. |
| P1 | Proposal recommendation is X | Question asks whether to adopt X; `Adopt X` is the recommended disposition with matching polarity. |
| P2 | User selects `Revise` with one changed parameter | Keeps the node open, applies the change, and recomputes first; asks only for still-needed inputs if the node remains reachable. |
| P3 | User selects `Reject / compare` | Transitions the same node to a complete alternatives presentation without re-pushing X. |
| P4 | User repeats a condition already stated in the proposal while adopting | Treats it as exact adoption, settles the node, and recomputes dependent nodes. |
| P5 | User adds or changes a condition while saying "adopt" | Treats the response as `Revise`, keeps the node open, and requests disposition on the revised proposal. |
| P6 | Exactly one feasible direction survives | Proposal states `No credible live alternative` and identifies the nearest constraint-eliminated candidate without presenting it as viable. |
| A1 | Five materially distinct viable options exist | Text rendering includes all five; no truncation, fake grouping, or tournament. |
| A2 | Only one direction survives hard constraints | Does not invent alternatives; explains eliminations and offers constraint reconsideration. |
| A3 | Two compatible toggles were drafted as exclusive options | Splits them into independent nodes or models a genuine combined strategy. |
| B1 | Four light independent questions share context | May ask all four in one text round if the one-read cognitive-load test passes. |
| B2 | Two independent but difficult, high-consequence questions are ready | Splits them into focused rounds despite frontier width of two. |
| B3 | The second ready-looking question can change after the first answer | Treats it as dependent; asks only the first and recomputes. |
| T1 | Two questions each have three mutually exclusive real options, every question has an honest leader, and the tool is available | Uses `request_user_input`; each recommended real option appears first. |
| T2 | One question has four viable options | Uses structured text even when the tool is available. |
| T3 | Four easy questions form one coherent natural round | Uses structured text instead of splitting solely for the picker limit. |
| T4 | Tool returns an empty answer object | Leaves nodes unanswered and re-presents the same decisions in text. |
| T5 | Alternatives have no honest leader because a user preference is missing | States the missing preference and uses structured text; the picker is inapplicable. |
| T6 | A picker-eligible round has full context and an honest leader | Presents the complete reasoning before the picker; the picker collects only the selection or disposition. |
| T7 | A multi-question result contains both answered and empty entries | Applies non-empty answers, recomputes the full tree, and re-presents only still-valid unanswered nodes. |
| S1 | An answer removes two queued nodes and unblocks a new one | Recomputes the whole tree and asks the new frontier, not the stale queue. |
| S2 | New verified evidence triggers a recorded reopen condition | Explicitly reopens the affected settled node and recomputes descendants. |
| F1 | Every reachable node is settled or pruned and no investigation or `blocked-on-fact` node remains | Summarizes decisions and asks confirm, revise, or pause before action. |
| F2 | User confirms the design but did not authorize publication | Ends grilling without publishing or inferring broader permission. |
| F3 | Ready frontier is empty because all reachable nodes are `blocked-on-fact` | Does not finish; continues or awaits the pending investigations. |
