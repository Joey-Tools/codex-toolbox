from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "personal_codex" / "skills" / "grilling"


class GrillingSkillContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.skill_words = " ".join(cls.skill.split())
        cls.interaction_modes = (
            SKILL_ROOT / "references" / "interaction-modes.md"
        ).read_text(encoding="utf-8")
        cls.interaction_mode_words = " ".join(cls.interaction_modes.split())
        cls.metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        cls.behavioral_matrix = {}
        for line in cls.interaction_modes.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) != 3 or not re.fullmatch(r"[A-Z]\d+", cells[0]):
                continue
            cls.behavioral_matrix[cells[0]] = {
                "setup": " ".join(cells[1].split()),
                "expected": " ".join(cells[2].split()),
            }

    def assert_matrix_behavior(
        self, setup_fragment: str, *expected_fragments: str
    ) -> None:
        matches = [
            row
            for row in self.behavioral_matrix.values()
            if setup_fragment in row["setup"]
        ]
        self.assertEqual(
            len(matches),
            1,
            f"expected one behavioral-matrix row containing {setup_fragment!r}",
        )
        expected = matches[0]["expected"]
        for fragment in expected_fragments:
            self.assertIn(fragment, expected)

    def test_skill_is_explicit_only(self) -> None:
        self.assertIn(
            "Use only when the user explicitly invokes `$grilling`.",
            self.skill_words,
        )
        self.assertIn(
            'default_prompt: "Use $grilling to stress-test this plan',
            self.metadata,
        )
        self.assertIn("allow_implicit_invocation: false", self.metadata)

    def test_adaptive_router_is_documented(self) -> None:
        self.assertIn("Choose a presentation independently", self.skill_words)
        self.assertIn("Use **alternatives mode**", self.skill_words)
        self.assertIn(
            "Automatically use **proposal mode** only when",
            self.skill_words,
        )
        self.assertIn("When the route is uncertain", self.skill_words)
        self.assertIn("protected decisions", self.skill_words)

    def test_blocked_fact_candidates_do_not_enter_the_ready_frontier(self) -> None:
        routing_contract = (
            "Evaluate the rows from top to bottom for each reachable candidate "
            "node while computing the decision frontier. First mark a node that "
            "needs accessible evidence as `blocked-on-fact`; only `ready` nodes "
            "whose prerequisites are settled enter the frontier and continue "
            "through the presentation rows"
        )
        self.assertIn(routing_contract, self.interaction_mode_words)
        self.assert_matrix_behavior(
            "Routing evidence is accessible but not yet known",
            "Investigates instead of asking the user",
            "unrelated frontier nodes can continue",
        )

    def test_explicit_proposal_override_changes_only_presentation(self) -> None:
        combined = f"{self.skill_words} {self.interaction_mode_words}"
        self.assertIn(
            "The override changes presentation only: it does not fabricate "
            "dominance, turn an assumption into a settled priority, or relax the "
            "fact, context, confirmation, or authorization invariants",
            combined,
        )
        self.assert_matrix_behavior(
            "proposal for a protected decision with no settled dominant direction",
            "presentation-only",
            "does not fabricate dominance or authorization",
        )

    def test_reject_creates_a_more_specific_alternatives_override(self) -> None:
        self.assertIn(
            "creates a newer, node-scoped alternatives override",
            self.interaction_mode_words,
        )
        self.assertIn(
            "outranks an older round- or session-wide proposal preference",
            self.interaction_mode_words,
        )
        self.assert_matrix_behavior(
            "Session proposal preference exists, then the user rejects one proposal",
            "newer node-scoped alternatives override",
            "instead of re-pushing the proposal",
        )

    def test_proposal_contract_preserves_decision_context(self) -> None:
        for required_section in (
            "Problem and timing",
            "Relevant context",
            "Proposal",
            "Why it leads",
            "Tradeoffs",
            "Strongest alternative",
            "Impact and next action",
            "Reopen condition",
        ):
            self.assertIn(required_section, self.skill)

        self.assertIn("**Adopt (Recommended)**", self.skill)
        self.assertIn("**Revise**", self.skill)
        self.assertIn("**Reject / compare alternatives**", self.skill)
        self.assertIn("Proposal mode is not a short-answer mode", self.skill_words)

    def test_alternatives_and_round_width_are_not_artificially_capped(self) -> None:
        self.assertIn("no fixed question count", self.skill_words)
        self.assertIn("no fixed option count", self.skill_words)
        self.assertIn("every materially distinct viable alternative", self.skill_words)
        self.assertIn("read it once and answer without rereading", self.skill_words)
        self.assertNotIn(
            "exactly three mutually exclusive choices", self.skill_words
        )

    def test_alternatives_without_an_honest_leader_use_text(self) -> None:
        self.assertIn(
            "If no option leads without a missing preference, state that missing "
            "preference instead of fabricating a recommendation",
            self.skill_words,
        )
        self.assertIn(
            "the picker is inapplicable and the comparison must remain in "
            "structured text",
            self.interaction_mode_words,
        )
        self.assert_matrix_behavior(
            "Alternatives have no honest leader because a user preference is missing",
            "States the missing preference and uses structured text",
            "picker is inapplicable",
        )

    def test_picker_limits_do_not_change_semantics(self) -> None:
        self.assertIn("one to three questions", self.skill_words)
        self.assertIn("two or three mutually exclusive choices", self.skill_words)
        self.assertIn("transport convenience", self.skill_words)
        self.assertIn(
            "use structured text even when the tool is available",
            self.skill_words,
        )
        self.assertIn("Do not add an `Other` option", self.skill_words)
        self.assertIn(
            "returns an empty `answers` object",
            self.skill_words,
        )
        self.assertIn(
            "Never set or describe a timeout, countdown",
            self.skill_words,
        )
        self.assertIn(
            "Do not change modes merely because",
            self.skill_words,
        )

    def test_picker_and_finish_matrix_setups_include_their_guard_conditions(
        self,
    ) -> None:
        self.assertEqual(
            self.behavioral_matrix["T1"]["setup"],
            "Two questions each have three mutually exclusive real options, "
            "every question has an honest leader, and the tool is available",
        )
        self.assertEqual(
            self.behavioral_matrix["T1"]["expected"],
            "Uses `request_user_input`; each recommended real option appears first.",
        )
        self.assertEqual(
            self.behavioral_matrix["F1"]["setup"],
            "Every reachable node is settled or pruned and no investigation or "
            "`blocked-on-fact` node remains",
        )
        self.assertEqual(
            self.behavioral_matrix["F1"]["expected"],
            "Summarizes decisions and asks confirm, revise, or pause before action.",
        )

    def test_full_reasoning_precedes_picker_and_picker_only_collects_answer(
        self,
    ) -> None:
        render_instruction = (
            "First present the complete proposal brief or alternatives comparison "
            "in the user-visible conversation"
        )
        collection_instruction = (
            "then use the picker only to collect the disposition or selection"
        )
        self.assertIn(render_instruction, self.skill_words)
        self.assertIn(collection_instruction, self.skill_words)
        self.assertLess(
            self.skill_words.index(render_instruction),
            self.skill_words.index(collection_instruction),
        )
        self.assert_matrix_behavior(
            "picker-eligible round has full context and an honest leader",
            "complete reasoning before the picker",
            "picker collects only the selection or disposition",
        )

    def test_answer_transitions_and_final_confirmation_are_explicit(self) -> None:
        self.assertIn("A revision is not partial adoption", self.skill_words)
        self.assertIn("do not reword and push the same proposal", self.skill_words)
        self.assertIn("After every answer round, recompute", self.skill_words)
        self.assertIn("Confirm and proceed (Recommended)", self.skill_words)
        self.assertIn("does not authorize unrelated external actions", self.skill_words)

    def test_pending_fact_blocks_completion(self) -> None:
        self.assertIn("no pending investigation", self.skill_words)
        self.assertIn("`blocked-on-fact` node", self.skill_words)
        self.assertIn("An empty ready frontier alone is not completion", self.skill_words)
        self.assert_matrix_behavior(
            "Ready frontier is empty because all reachable nodes are `blocked-on-fact`",
            "Does not finish",
            "pending investigations",
        )

    def test_sole_feasible_direction_need_not_invent_an_alternative(self) -> None:
        self.assertIn("No credible live alternative", self.interaction_mode_words)
        self.assertIn("nearest eliminated candidate", self.interaction_mode_words)
        self.assert_matrix_behavior(
            "Exactly one feasible direction survives",
            "No credible live alternative",
            "nearest constraint-eliminated candidate",
            "without presenting it as viable",
        )

    def test_conditional_adoption_is_revision(self) -> None:
        self.assertIn(
            "Treat a newly added or changed condition on a proposal as **Revise**, "
            "not **Adopt**",
            self.skill_words,
        )
        self.assert_matrix_behavior(
            'User adds or changes a condition while saying "adopt"',
            "Treats the response as `Revise`",
            "keeps the node open",
            "revised proposal",
        )

    def test_revision_applies_changes_and_recomputes_before_follow_up(self) -> None:
        revision_rule = (
            "On **Revise**, keep the proposal node open, apply the supplied "
            "changes, and recompute the entire tree before asking anything else. "
            "If the node remains reachable and needs more input, ask only for the "
            "minimum missing parameters needed to reformulate it"
        )
        self.assertIn(revision_rule, self.skill_words)
        self.assert_matrix_behavior(
            "User selects `Revise` with one changed parameter",
            "applies the change",
            "recomputes first",
            "only for still-needed inputs if the node remains reachable",
        )

    def test_partial_answers_are_applied_before_recomputing_and_representing(
        self,
    ) -> None:
        apply_instruction = "apply all non-empty answers"
        recompute_instruction = "recompute the entire tree"
        represent_instruction = (
            "re-present only still-reachable, still-valid unanswered nodes"
        )
        for instruction in (
            apply_instruction,
            recompute_instruction,
            represent_instruction,
        ):
            self.assertIn(instruction, self.interaction_mode_words)

        partial_answer_rule = self.interaction_mode_words[
            self.interaction_mode_words.index(apply_instruction) :
        ]
        self.assertLess(
            partial_answer_rule.index(apply_instruction),
            partial_answer_rule.index(recompute_instruction),
        )
        self.assertLess(
            partial_answer_rule.index(recompute_instruction),
            partial_answer_rule.index(represent_instruction),
        )
        self.assert_matrix_behavior(
            "multi-question result contains both answered and empty entries",
            "Applies non-empty answers",
            "recomputes the full tree",
            "still-valid unanswered nodes",
        )

    def test_interaction_reference_covers_behavioral_boundaries(self) -> None:
        self.assertIn(
            "[references/interaction-modes.md]",
            self.skill,
        )
        self.assertIn(
            "There is no fixed minimum or maximum option count",
            self.interaction_mode_words,
        )
        self.assertIn(
            "There is no fixed question count",
            self.interaction_mode_words,
        )
        self.assertIn("one to three questions", self.interaction_mode_words)
        self.assertIn("two or three real answer options", self.interaction_mode_words)

        self.assertTrue(
            {
                "R1",
                "R3",
                "P2",
                "P3",
                "A1",
                "B1",
                "B2",
                "T2",
                "T4",
                "S1",
                "F2",
            }.issubset(self.behavioral_matrix)
        )

    def test_public_manifest_distributes_skill(self) -> None:
        manifest = json.loads(
            (REPO_ROOT / "personal_codex" / "public-sync-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            {
                "source": "personal_codex/skills/grilling",
                "target": "skills/grilling",
                "kind": "skill",
            },
            manifest["links"],
        )
