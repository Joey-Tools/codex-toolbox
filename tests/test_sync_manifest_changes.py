from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_sync_manifest_changes.py"
SPEC = importlib.util.spec_from_file_location("validate_sync_manifest_changes", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def manifest(*skills: str, removed_links: list[dict[str, object]] | None = None):
    data: dict[str, object] = {
        "version": 1,
        "links": [
            {
                "source": f"personal_codex/skills/{skill}",
                "target": f"skills/{skill}",
                "kind": "skill",
            }
            for skill in skills
        ],
        "reference_only": [],
    }
    if removed_links is not None:
        data["removed_links"] = removed_links
    return data


def removed(skill: str, removed_id: str, *, legacy: bool = False):
    return {
        "id": removed_id,
        "source": f"personal_codex/skills/{skill}",
        "target": f"skills/{skill}",
        "kind": "skill",
        "legacy": legacy,
    }


class SyncManifestChangeTests(unittest.TestCase):
    def test_rejects_non_integer_manifest_versions(self) -> None:
        for version in (True, 1.0):
            data = manifest()
            data["version"] = version
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(MODULE.ValidationError, "version must be 1"),
            ):
                MODULE._manifest_model(data)

    def test_manifest_active_link_limit_reserves_current_transaction_record(
        self,
    ) -> None:
        self.assertEqual(
            MODULE.MAX_MANIFEST_ACTIVE_LINKS,
            min(
                MODULE.MAX_PENDING_LINK_RECORDS,
                MODULE.MAX_PENDING_LINK_CLAIMS,
            )
            - 1,
        )
        self.assertEqual(MODULE.MAX_MANIFEST_ACTIVE_LINKS, 9_999)

        with mock.patch.object(MODULE, "MAX_MANIFEST_ACTIVE_LINKS", 2):
            model = MODULE._manifest_model(manifest("one", "two"))
            self.assertEqual(len(model["links"]), 2)
            with self.assertRaisesRegex(
                MODULE.ValidationError,
                "active links exceed runtime transaction limit: 3 > 2",
            ):
                MODULE._manifest_model(manifest("one", "two", "three"))

    def test_manifest_transition_limit_reserves_current_record(self) -> None:
        previous = manifest("old-one", "old-two")
        current = manifest(
            "new-one",
            "new-two",
            removed_links=[
                removed("old-one", "remove-old-one"),
                removed("old-two", "remove-old-two"),
            ],
        )
        with mock.patch.object(MODULE, "MAX_PENDING_LINK_RECORDS", 5):
            MODULE.validate_manifest_change(previous, current)

        previous = manifest("old-one", "old-two", "old-three")
        current["removed_links"].append(
            removed("old-three", "remove-old-three")
        )
        with (
            mock.patch.object(MODULE, "MAX_PENDING_LINK_RECORDS", 5),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "declared history exceeds runtime transaction limit: 6 > 5",
            ),
        ):
            MODULE.validate_manifest_change(previous, current)

    def test_manifest_transition_counts_same_target_change_once(self) -> None:
        skills = ("one", "two", "three", "four")
        previous = manifest(*skills)
        current = manifest(
            *skills,
            removed_links=[
                removed(skill, f"replace-{skill}") for skill in skills
            ],
        )
        for skill, entry in zip(skills, current["links"]):
            entry["source"] = f"personal_codex/skills/replacement-{skill}"

        with mock.patch.object(MODULE, "MAX_PENDING_LINK_RECORDS", 5):
            MODULE.validate_manifest_change(previous, current)

    def test_manifest_transition_byte_capacity_has_exact_boundary(self) -> None:
        previous = MODULE._manifest_model(manifest("skill"))
        current_raw = manifest("skill")
        current_raw["links"][0]["source"] = (
            "personal_codex/skills/replacement-skill"
        )
        current = MODULE._manifest_model(current_raw)
        runtime = MODULE._sync_runtime_module()
        projected_size = runtime._manifest_transition_metadata_size(
            MODULE._transition_capacity_profile(previous),
            MODULE._transition_capacity_profile(current),
        )

        with mock.patch.object(
            runtime,
            "MAX_MANAGED_STATE_BYTES",
            projected_size,
        ):
            MODULE._validate_transition_capacity(previous, current)

        release_sha = "a" * 40
        with (
            mock.patch.object(
                runtime,
                "MAX_MANAGED_STATE_BYTES",
                projected_size - 1,
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                f"byte capacity from release {release_sha}",
            ),
        ):
            MODULE._validate_transition_capacity(
                previous,
                current,
                release_sha=release_sha,
            )

    def test_manifest_transition_capacity_reuses_current_profile(self) -> None:
        previous_one = MODULE._manifest_model(manifest("previous-one"))
        previous_two = MODULE._manifest_model(manifest("previous-two"))
        current = MODULE._manifest_model(manifest("current"))
        runtime = MODULE._sync_runtime_module()

        with mock.patch.object(
            runtime,
            "_manifest_transition_capacity_profile",
            wraps=runtime._manifest_transition_capacity_profile,
        ) as build_profile:
            MODULE._validate_transition_capacity(previous_one, current)
            MODULE._validate_transition_capacity(previous_two, current)

        self.assertEqual(build_profile.call_count, 3)
        self.assertIn(MODULE._TRANSITION_CAPACITY_PROFILE_KEY, current)
        with mock.patch.object(
            runtime,
            "_managed_state_bytes",
            side_effect=AssertionError("managed state was re-encoded"),
        ):
            MODULE._validate_transition_capacity(previous_one, current)

    def test_removed_history_contributes_worst_target_record_size(self) -> None:
        target = "skills/retired"
        short_history = removed("short", "remove-short", legacy=True)
        short_history["target"] = target
        long_history = removed("long", "remove-long", legacy=True)
        long_history["source"] = (
            "personal_codex/skills/"
            + "/".join(["long-component"] * 32)
        )
        long_history["target"] = target
        runtime = MODULE._sync_runtime_module()

        def profile_for(
            removed_links: list[dict[str, object]],
        ):
            return MODULE._transition_capacity_profile(
                MODULE._manifest_model(
                    manifest("current", removed_links=removed_links)
                )
            )

        no_history = profile_for([])
        short_profile = profile_for([short_history])
        long_profile = profile_for([long_history])
        combined_profile = profile_for([short_history, long_history])
        target_path = runtime.PurePosixPath(target)

        self.assertEqual(
            combined_profile.historical_record_sizes[target_path],
            max(
                short_profile.historical_record_sizes[target_path],
                long_profile.historical_record_sizes[target_path],
            ),
        )
        no_history_size = runtime._manifest_transition_metadata_size(
            no_history,
            no_history,
        )
        combined_size = runtime._manifest_transition_metadata_size(
            no_history,
            combined_profile,
        )
        self.assertGreater(combined_size, no_history_size)
        previous = MODULE._manifest_model(manifest("current"))
        current = MODULE._manifest_model(
            manifest(
                "current",
                removed_links=[short_history, long_history],
            )
        )
        with (
            mock.patch.object(
                runtime,
                "MAX_MANAGED_STATE_BYTES",
                combined_size - 1,
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "transaction byte capacity",
            ),
        ):
            MODULE._validate_transition_capacity(previous, current)

    def test_projected_array_size_tracks_index_digits_and_newline(self) -> None:
        runtime = MODULE._sync_runtime_module()
        base_item = {"index": 0, "nested": {"value": "value"}}
        count = 101
        items = [
            {"index": index, "nested": {"value": "value"}}
            for index in range(count)
        ]
        empty_size = runtime._projected_json_size(
            {"items": []},
            trailing_newline=False,
        )
        full_size = runtime._projected_json_size(
            {"items": items},
            trailing_newline=False,
        )
        element_size_sum = count * (
            runtime._projected_top_level_array_element_size(base_item)
        )

        self.assertEqual(runtime._projected_index_digit_delta(10), 0)
        self.assertEqual(runtime._projected_index_digit_delta(11), 1)
        self.assertEqual(runtime._projected_index_digit_delta(100), 90)
        self.assertEqual(runtime._projected_index_digit_delta(101), 92)
        self.assertEqual(
            full_size - empty_size,
            runtime._projected_top_level_array_delta(
                element_size_sum,
                count,
                indexed=True,
            ),
        )
        self.assertEqual(
            runtime._projected_json_size(items, trailing_newline=True),
            runtime._projected_json_size(items, trailing_newline=False) + 1,
        )

    def test_manifest_transition_projection_bounds_repair_variants(self) -> None:
        model = MODULE._manifest_model(manifest("one", "two", "three"))
        runtime = MODULE._sync_runtime_module()
        profile = MODULE._transition_capacity_profile(model)
        projected_size = runtime._manifest_transition_metadata_size(
            profile,
            profile,
        )
        home = Path("/home/codex/.codex")
        owner = profile.owner
        release_sha = profile.state.owners[owner]
        targets = sorted(profile.state.links, key=lambda path: path.as_posix())

        for repair_mask in range(1 << len(targets)):
            actions = [
                (
                    "current",
                    runtime.ReconcileAction(
                        "replace",
                        runtime._current_link(home, owner),
                        f"releases/{release_sha}",
                        "directory",
                        expected_link_target=runtime._MAX_PENDING_LINK_TARGET,
                    ),
                )
            ]
            for index, target in enumerate(targets):
                if not repair_mask & (1 << index):
                    continue
                record = profile.state.links[target]
                actions.append(
                    (
                        "managed",
                        runtime.ReconcileAction(
                            "create",
                            home / Path(*target.parts),
                            record.link_target,
                            record.kind,
                            planned_snapshot=runtime.ReconcileTargetSnapshot(
                                parent_identity=runtime._MAX_PENDING_IDENTITY,
                            ),
                        ),
                    )
                )
            records = []
            record_actions = {}
            for scope, action in actions:
                target = runtime.PurePosixPath(
                    *action.target.relative_to(home).parts
                )
                record_actions[(scope, target)] = action.action
                records.append(
                    runtime._projected_pending_record_payload(
                        home,
                        scope,
                        action,
                        profile.state,
                        profile.state,
                        profile.state,
                        len(records),
                    )
                )
            payload = runtime._projected_pending_metadata_payload(
                state_before_exists=True,
                records=records,
                claims_before=runtime._projected_pending_claim_payloads(
                    home,
                    "before",
                    profile.state,
                    record_actions,
                ),
                claims_after=runtime._projected_pending_claim_payloads(
                    home,
                    "after",
                    profile.state,
                    record_actions,
                ),
                releases_before=runtime._projected_pending_release_payloads(
                    profile.state
                ),
                releases_after=runtime._projected_pending_release_payloads(
                    profile.state
                ),
            )
            actual_size = runtime._projected_json_size(
                payload,
                trailing_newline=True,
            )

            with self.subTest(repair_mask=repair_mask):
                self.assertGreaterEqual(projected_size, actual_size)

    def test_manifest_transition_projection_bounds_mixed_actions(self) -> None:
        unchanged = tuple(f"keep-{index:02d}" for index in range(11))
        previous_raw = manifest(*unchanged, "changed", "deleted")
        current_raw = manifest(
            *unchanged,
            "changed",
            "added",
            removed_links=[
                removed("changed", "replace-changed"),
                removed("deleted", "remove-deleted"),
                removed("legacy", "remove-legacy", legacy=True),
            ],
        )
        for entry in current_raw["links"]:
            if entry["target"] == "skills/changed":
                entry["source"] = "personal_codex/skills/replacement-changed"
        previous_model = MODULE._manifest_model(previous_raw)
        current_model = MODULE._manifest_model(current_raw)
        runtime = MODULE._sync_runtime_module()
        previous = MODULE._transition_capacity_profile(previous_model)
        current = MODULE._transition_capacity_profile(current_model)
        projected_size = runtime._manifest_transition_metadata_size(
            previous,
            current,
        )
        home = Path("/home/codex/.codex")
        owner = current.owner
        release_sha = current.state.owners[owner]
        unchanged_targets = [
            runtime.PurePosixPath(f"skills/{skill}") for skill in unchanged
        ]
        changed_target = runtime.PurePosixPath("skills/changed")
        deleted_target = runtime.PurePosixPath("skills/deleted")
        added_target = runtime.PurePosixPath("skills/added")
        legacy_target = runtime.PurePosixPath("skills/legacy")

        def create_action(target):
            record = current.state.links[target]
            return runtime.ReconcileAction(
                "create",
                home / Path(*target.parts),
                record.link_target,
                record.kind,
                planned_snapshot=runtime.ReconcileTargetSnapshot(
                    parent_identity=runtime._MAX_PENDING_IDENTITY,
                ),
            )

        def destructive_action(
            action_name,
            target,
            expected_link_target,
        ):
            current_record = current.state.links.get(target)
            previous_record = previous.state.links.get(target)
            record = current_record or previous_record
            assert record is not None
            return runtime.ReconcileAction(
                action_name,
                home / Path(*target.parts),
                current_record.link_target if current_record is not None else "",
                record.kind,
                expected_link_target=expected_link_target,
                planned_snapshot=runtime.ReconcileTargetSnapshot(
                    parent_identity=runtime._MAX_PENDING_IDENTITY,
                    link_identity=runtime._MAX_PENDING_IDENTITY,
                    link_target=expected_link_target,
                    ancestor_identity=runtime._MAX_PENDING_IDENTITY,
                ),
            )

        removed_by_target = {
            runtime.PurePosixPath(entry["target"]): entry
            for entry in current_model["removed"].values()
        }

        def historical_target(target):
            entry = removed_by_target[target]
            return runtime._desired_link_target(
                home,
                runtime.LinkEntry(
                    source=runtime.PurePosixPath(entry["source"]),
                    target=target,
                    kind=entry["kind"],
                    owner=owner,
                ),
            )

        def quarantine_remove(target):
            entry = removed_by_target[target]
            expected = historical_target(target)
            return runtime.ReconcileAction(
                "quarantine-remove",
                home / Path(*target.parts),
                "",
                entry["kind"],
                expected_link_target=expected,
                planned_snapshot=runtime.ReconcileTargetSnapshot(
                    parent_identity=runtime._MAX_PENDING_IDENTITY,
                    link_identity=runtime._MAX_PENDING_IDENTITY,
                    link_target=expected,
                    ancestor_identity=runtime._MAX_PENDING_IDENTITY,
                ),
            )

        def actual_size(
            state_before,
            managed_actions,
            retired_absence_specs=(),
        ):
            current_action = runtime.ReconcileAction(
                "replace",
                runtime._current_link(home, owner),
                f"releases/{release_sha}",
                "directory",
                expected_link_target=runtime._MAX_PENDING_LINK_TARGET,
            )
            flattened_actions = (current_action, *managed_actions)
            capacity = runtime.PendingLinkCapacityPlan(
                ordered_groups=(
                    ("current", (current_action,)),
                    ("managed", tuple(managed_actions)),
                ),
                flattened_actions=flattened_actions,
                retired_absence_specs=tuple(retired_absence_specs),
            )
            captured = []

            def capture(payload, **_kwargs):
                captured.append(payload)
                return b""

            with mock.patch.object(
                runtime,
                "_bounded_json_document",
                side_effect=capture,
            ):
                runtime._validate_pending_link_metadata_capacity(
                    home,
                    capacity,
                    runtime.ManagedStateFileSnapshot(exists=True),
                    state_before,
                    state_before,
                    current.state,
                )
            self.assertEqual(len(captured), 1)
            return len(
                (
                    json.dumps(captured[0], indent=2, sort_keys=False)
                    + "\n"
                ).encode("utf-8")
            )

        canonical_actions = [
            *(create_action(target) for target in unchanged_targets[::2]),
            destructive_action(
                "replace",
                changed_target,
                previous.state.links[changed_target].link_target,
            ),
            destructive_action(
                "remove",
                deleted_target,
                previous.state.links[deleted_target].link_target,
            ),
            create_action(added_target),
            quarantine_remove(legacy_target),
        ]
        retired_actions = [
            *(create_action(target) for target in unchanged_targets[1::2]),
            destructive_action(
                "replace",
                changed_target,
                previous.state.links[changed_target].link_target,
            ),
            create_action(added_target),
            quarantine_remove(legacy_target),
        ]
        quarantine_actions = [
            create_action(added_target),
            destructive_action(
                "quarantine-replace",
                changed_target,
                historical_target(changed_target),
            ),
            quarantine_remove(deleted_target),
            quarantine_remove(legacy_target),
        ]
        quarantine_state = runtime.ManagedState(
            owners=previous.state.owners,
            links={
                target: record
                for target, record in previous.state.links.items()
                if target not in {changed_target, deleted_target}
            },
        )
        actual_sizes = (
            actual_size(previous.state, canonical_actions),
            actual_size(
                previous.state,
                retired_actions,
                (
                    (
                        deleted_target,
                        previous.state.links[deleted_target],
                    ),
                ),
            ),
            actual_size(quarantine_state, quarantine_actions),
        )

        for scenario, actual in zip(
            ("canonical", "retired-absence", "quarantine"),
            actual_sizes,
        ):
            with self.subTest(scenario=scenario):
                self.assertGreaterEqual(projected_size, actual)

    def test_requires_removed_link_for_deleted_target(self) -> None:
        previous = manifest("keep", "retired")
        current = manifest("keep")

        with self.assertRaisesRegex(MODULE.ValidationError, "requires one new matching"):
            MODULE.validate_manifest_change(previous, current)

    def test_accepts_matching_removed_link(self) -> None:
        previous = manifest("keep", "retired")
        current = manifest(
            "keep",
            removed_links=[removed("retired", "remove-retired")],
        )

        MODULE.validate_manifest_change(previous, current)

    def test_requires_removed_link_for_same_target_identity_change(self) -> None:
        for field, value in (
            ("source", "personal_codex/skills/replacement"),
            ("kind", "directory"),
        ):
            with self.subTest(field=field):
                previous = manifest("keep")
                current = manifest("keep")
                current["links"][0][field] = value

                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "requires one new matching",
                ):
                    MODULE.validate_manifest_change(previous, current)

    def test_accepts_removed_link_for_same_target_source_change(self) -> None:
        previous = manifest("keep")
        current = manifest(
            "keep",
            removed_links=[removed("keep", "replace-keep-source")],
        )
        current["links"][0]["source"] = "personal_codex/skills/replacement"

        MODULE.validate_manifest_change(previous, current)

    def test_preserves_existing_removed_link_history(self) -> None:
        previous = manifest(
            "keep",
            removed_links=[removed("old", "remove-old")],
        )
        current = manifest("keep", removed_links=[])

        with self.assertRaisesRegex(MODULE.ValidationError, "changed or disappeared"):
            MODULE.validate_manifest_change(previous, current)

    def test_rejects_unknown_removed_link_fields_in_all_history_states(self) -> None:
        known = removed("old", "remove-old", legacy=True)
        with_unknown = {**known, "future_note": "first"}
        changed_unknown = {**known, "future_note": "second"}
        cases = (
            (manifest("keep"), manifest("keep", removed_links=[with_unknown])),
            (
                manifest("keep", removed_links=[with_unknown]),
                manifest("keep", removed_links=[changed_unknown]),
            ),
            (
                manifest("keep", removed_links=[with_unknown]),
                manifest("keep", removed_links=[known]),
            ),
        )

        for previous, current in cases:
            with self.subTest(previous=previous, current=current), self.assertRaisesRegex(
                MODULE.ValidationError,
                "unsupported field",
            ):
                MODULE.validate_manifest_change(previous, current)

    def test_allows_explicit_legacy_removed_link(self) -> None:
        previous = manifest("keep")
        current = manifest(
            "keep",
            removed_links=[removed("orphan", "remove-orphan", legacy=True)],
        )

        MODULE.validate_manifest_change(previous, current)

    def test_rejects_unexplained_removed_link_without_legacy(self) -> None:
        previous = manifest("keep")
        current = manifest(
            "keep",
            removed_links=[removed("orphan", "remove-orphan")],
        )

        with self.assertRaisesRegex(MODULE.ValidationError, "legacy=true"):
            MODULE.validate_manifest_change(previous, current)

    def test_requires_new_id_for_second_removal_episode(self) -> None:
        old = removed("retired", "first-removal")
        previous = manifest("keep", "retired", removed_links=[old])
        current = manifest("keep", removed_links=[old])

        with self.assertRaisesRegex(MODULE.ValidationError, "requires one new matching"):
            MODULE.validate_manifest_change(previous, current)

    def test_replacement_target_must_be_safe(self) -> None:
        entry = removed("retired", "rename-retired")
        entry["replacement_target"] = "../skills/replacement"
        current = manifest("keep", removed_links=[entry])

        with self.assertRaisesRegex(MODULE.ValidationError, "safe relative path"):
            MODULE._manifest_model(current)

    def test_public_replacement_target_must_be_active_or_retired(self) -> None:
        previous = manifest("keep", "retired")
        rename = removed("retired", "rename-retired")
        rename["replacement_target"] = "skills/replacement"
        current = manifest("keep", removed_links=[rename])

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "replacement target skills/replacement is unavailable",
        ):
            MODULE.validate_manifest_change(previous, current)

        current = manifest("keep", "replacement", removed_links=[rename])
        MODULE.validate_manifest_change(previous, current)

    def test_private_replacement_availability_is_resolved_by_release_set(
        self,
    ) -> None:
        previous = manifest("keep", "moving")
        previous["owner"] = "private"
        migration = removed("moving", "move-moving-to-public")
        migration["replacement_target"] = "skills/moving"
        current = manifest("keep", removed_links=[migration])
        current["owner"] = "private"

        MODULE.validate_manifest_change(previous, current)

    def test_accepts_explicit_same_owner_replacement_retirement(self) -> None:
        rename = removed("retired", "rename-retired")
        rename["replacement_target"] = "skills/replacement"
        retirement = removed("replacement", "remove-replacement")
        retirement["retires_replacements"] = ["public:rename-retired"]
        current = manifest(
            "keep",
            removed_links=[rename, retirement],
        )

        MODULE._manifest_model(current)

    def test_replacement_retirement_requires_owner_id_key(self) -> None:
        retirement = removed("replacement", "remove-replacement")
        retirement["retires_replacements"] = ["rename-retired"]
        current = manifest("keep", removed_links=[retirement])

        with self.assertRaisesRegex(MODULE.ValidationError, "owner:id"):
            MODULE._manifest_model(current)

    def test_same_owner_replacement_retirement_must_resolve(self) -> None:
        retirement = removed("replacement", "remove-replacement")
        retirement["retires_replacements"] = ["public:missing"]
        current = manifest("keep", removed_links=[retirement])

        with self.assertRaisesRegex(MODULE.ValidationError, "unknown replacement"):
            MODULE._manifest_model(current)

    def test_rejects_replacement_retirement_cycles(self) -> None:
        def retirement(node: str, replacement: str, retired: str):
            entry = removed(node, node)
            entry["replacement_target"] = f"skills/{replacement}"
            entry["retires_replacements"] = [f"public:{retired}"]
            return entry

        cases = (
            (
                "two-node",
                [
                    retirement("a", "b", "b"),
                    retirement("b", "a", "a"),
                ],
            ),
            (
                "multi-node",
                [
                    retirement("a", "c", "b"),
                    retirement("b", "a", "c"),
                    retirement("c", "b", "a"),
                ],
            ),
        )

        for label, removed_links in cases:
            with self.subTest(cycle=label), self.assertRaisesRegex(
                MODULE.ValidationError,
                "replacement retirement cycle",
            ):
                MODULE._manifest_model(manifest("keep", removed_links=removed_links))

    def test_manifest_paths_reject_collapsed_empty_segments(self) -> None:
        current = manifest("keep")
        current["links"][0]["source"] = "personal_codex//skills/keep"

        with self.assertRaisesRegex(MODULE.ValidationError, "safe relative path"):
            MODULE._manifest_model(current)

    def test_manifest_target_paths_enforce_byte_and_depth_limits(self) -> None:
        boundary = "/".join(["é" * 31] * 63 + ["é" * 63 + "x"])
        byte_overflow = boundary + "x"
        component_boundary = "é" * 127 + "x"
        component_overflow = component_boundary + "x"
        depth_overflow = "/".join(
            "x" for _ in range(MODULE.MAX_MANIFEST_TARGET_PATH_DEPTH + 1)
        )
        self.assertEqual(
            len(boundary.encode("utf-8")),
            MODULE.MAX_MANIFEST_TARGET_PATH_BYTES,
        )
        self.assertEqual(
            len(Path(boundary).parts),
            MODULE.MAX_MANIFEST_TARGET_PATH_DEPTH,
        )
        self.assertEqual(
            len(component_boundary.encode("utf-8")),
            MODULE.MAX_MANIFEST_TARGET_COMPONENT_BYTES,
        )

        def payload(route: str, target: str) -> dict[str, object]:
            if route == "active":
                current = manifest("keep")
                current["links"][0]["target"] = target
                return current
            entry = removed("retired", "remove-retired")
            if route == "removed":
                entry["target"] = target
            else:
                entry["replacement_target"] = target
            current = manifest("keep", removed_links=[entry])
            if route == "replacement":
                current["links"][0]["target"] = target
            return current

        for route in ("active", "removed", "replacement"):
            with self.subTest(route=route, limit="boundary"):
                MODULE._manifest_model(payload(route, boundary))
            with self.subTest(route=route, limit="bytes"):
                with self.assertRaisesRegex(MODULE.ValidationError, "UTF-8 bytes"):
                    MODULE._manifest_model(payload(route, byte_overflow))
            with self.subTest(route=route, limit="component-boundary"):
                MODULE._manifest_model(payload(route, component_boundary))
            with self.subTest(route=route, limit="component"):
                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "component 1",
                ):
                    MODULE._manifest_model(payload(route, component_overflow))
            with self.subTest(route=route, limit="depth"):
                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "path components",
                ):
                    MODULE._manifest_model(payload(route, depth_overflow))

        for label, target, message in (
            ("bytes", byte_overflow, "UTF-8 bytes"),
            ("component", component_overflow, "component 1"),
            ("depth", depth_overflow, "path components"),
        ):
            with (
                self.subTest(early_rejection=label),
                mock.patch.object(MODULE, "_portable_target_key") as portable_key,
                self.assertRaisesRegex(MODULE.ValidationError, message),
            ):
                MODULE._target_path(target, "target")
            portable_key.assert_not_called()

    def test_manifest_owner_and_link_policy_matches_runtime(self) -> None:
        inherited_private_owner = manifest("keep")
        inherited_private_owner["owner"] = "private"
        valid = (
            manifest("keep"),
            inherited_private_owner,
            {
                **manifest("keep"),
                "owner": "private",
                "links": [
                    {
                        **manifest("keep")["links"][0],
                        "owner": "private",
                        "override": True,
                    }
                ],
            },
        )
        for current in valid:
            with self.subTest(valid=current):
                MODULE._manifest_model(current)

        invalid = []
        for owner in (None, "", "private owner", "/private"):
            current = manifest("keep")
            current["owner"] = owner
            invalid.append(("manifest owner", current))

        mismatched_owner = manifest("keep")
        mismatched_owner["owner"] = "private"
        mismatched_owner["links"][0]["owner"] = "public"
        invalid.append(("does not match manifest owner", mismatched_owner))

        invalid_link_owner = manifest("keep")
        invalid_link_owner["links"][0]["owner"] = "private owner"
        invalid.append(("link owner", invalid_link_owner))

        null_link_owner = manifest("keep")
        null_link_owner["links"][0]["owner"] = None
        invalid.append(("link owner", null_link_owner))

        string_override = manifest("keep")
        string_override["links"][0]["override"] = "true"
        invalid.append(("override must be boolean", string_override))

        public_override = manifest("keep")
        public_override["links"][0]["override"] = True
        invalid.append(("public manifest links", public_override))

        for message, current in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(
                MODULE.ValidationError,
                message,
            ):
                MODULE._manifest_model(current)

    def test_override_policy_does_not_change_removal_identity(self) -> None:
        previous = manifest("keep")
        previous["owner"] = "private"
        current = manifest("keep")
        current["owner"] = "private"
        current["links"][0]["override"] = True

        MODULE.validate_manifest_change(previous, current)

    def test_base_release_schema_matches_runtime(self) -> None:
        valid = (
            None,
            {},
            {"repo": None, "sha": None},
            {"repo": "owner/repository"},
            {"sha": "a" * 40},
            {"repo": "owner/repository", "sha": "0123456789abcdef" * 2 + "01234567"},
        )
        for base_release in valid:
            current = manifest("keep")
            current["base_release"] = base_release
            with self.subTest(valid=base_release):
                MODULE._manifest_model(current)

        invalid_objects = ("owner/repository", [], 1, True)
        for base_release in invalid_objects:
            current = manifest("keep")
            current["base_release"] = base_release
            with self.subTest(base_release=base_release), self.assertRaisesRegex(
                MODULE.ValidationError,
                "base_release must be an object",
            ):
                MODULE._manifest_model(current)

        invalid_repositories = (
            "",
            "owner",
            "owner/repo/extra",
            "/",
            "/repo",
            "owner/",
            ".owner/repo",
            "owner/.repo",
            1,
            True,
            [],
        )
        for repository in invalid_repositories:
            current = manifest("keep")
            current["base_release"] = {"repo": repository}
            with self.subTest(repository=repository), self.assertRaisesRegex(
                MODULE.ValidationError,
                "base_release.repo must be an owner/repo string",
            ):
                MODULE._manifest_model(current)

        invalid_shas = (
            "a" * 39,
            "a" * 41,
            "A" * 40,
            "g" * 40,
            1,
            True,
            [],
        )
        for sha in invalid_shas:
            current = manifest("keep")
            current["base_release"] = {"sha": sha}
            with self.subTest(sha=sha), self.assertRaisesRegex(
                MODULE.ValidationError,
                "base_release.sha must be a 40-character lowercase hex SHA",
            ):
                MODULE._manifest_model(current)

    def test_manifest_source_kind_constraints_match_runtime(self) -> None:
        path_kinds = {
            "payload/file": "file",
            "payload/directory": "directory",
            "payload/skill": "directory",
            "payload/skill/SKILL.md": "file",
            "payload/reference": "file",
        }

        valid = {
            "version": 1,
            "links": [
                {
                    "source": "payload/file",
                    "target": "bin/file",
                    "kind": "file",
                },
                {
                    "source": "payload/directory",
                    "target": "directories/example",
                    "kind": "directory",
                },
                {
                    "source": "payload/skill",
                    "target": "skills/example",
                    "kind": "skill",
                },
            ],
            "reference_only": ["payload/reference"],
        }
        MODULE._manifest_model(valid, path_kinds.get, source_context="test tree")

        cases = []
        file_mismatch = manifest("keep")
        file_mismatch["links"][0] = {
            "source": "payload/directory",
            "target": "bin/file",
            "kind": "file",
        }
        cases.append(("file source", file_mismatch, path_kinds))

        directory_mismatch = manifest("keep")
        directory_mismatch["links"][0] = {
            "source": "payload/file",
            "target": "directories/example",
            "kind": "directory",
        }
        cases.append(("directory source", directory_mismatch, path_kinds))

        missing_skill_markdown = manifest("keep")
        missing_skill_markdown["links"][0] = {
            "source": "payload/directory",
            "target": "skills/example",
            "kind": "skill",
        }
        cases.append(("missing SKILL.md", missing_skill_markdown, path_kinds))

        missing_reference = manifest("keep")
        missing_reference["links"][0] = {
            "source": "payload/skill",
            "target": "skills/example",
            "kind": "skill",
        }
        missing_reference["reference_only"] = ["payload/missing"]
        cases.append(("reference_only path", missing_reference, path_kinds))

        for message, current, kinds in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                MODULE.ValidationError,
                message,
            ):
                MODULE._manifest_model(
                    current,
                    kinds.get,
                    source_context="test tree",
                )

    def test_manifest_rejects_sync_internal_targets(self) -> None:
        for internal_root in ("personal-sync", "Personal-Sync"):
            active = manifest("keep")
            active["links"][0]["target"] = (
                f"{internal_root}/state/managed-links.json"
            )
            removed_entry = removed("orphan", "remove-orphan", legacy=True)
            removed_entry["target"] = f"{internal_root}/quarantine/old-link"
            removed_manifest = manifest("keep", removed_links=[removed_entry])
            replacement_entry = removed("orphan", "replace-orphan", legacy=True)
            replacement_entry["replacement_target"] = f"{internal_root}/current"
            replacement_manifest = manifest("keep", removed_links=[replacement_entry])

            for label, current in (
                ("active", active),
                ("removed", removed_manifest),
                ("replacement", replacement_manifest),
            ):
                with self.subTest(root=internal_root, label=label):
                    with self.assertRaisesRegex(
                        MODULE.ValidationError,
                        "sync internal path",
                    ):
                        MODULE._manifest_model(current)

    def test_manifest_rejects_pending_transaction_targets(self) -> None:
        targets = (
            ".personal-sync-pending-transaction.json",
            ".Personal-Sync-Pending-Transaction.JSON",
            ".per\N{LATIN SMALL LETTER LONG S}onal-sync-pending-transaction.json",
            ".personal-sync-pending-transaction.json/child",
        )
        for target in targets:
            active = manifest("keep")
            active["links"][0]["target"] = target
            removed_entry = removed("orphan", "remove-orphan", legacy=True)
            removed_entry["target"] = target
            removed_manifest = manifest("keep", removed_links=[removed_entry])
            replacement_entry = removed(
                "orphan",
                "replace-orphan",
                legacy=True,
            )
            replacement_entry["replacement_target"] = target
            replacement_manifest = manifest(
                "keep",
                removed_links=[replacement_entry],
            )

            for label, current in (
                ("active", active),
                ("removed", removed_manifest),
                ("replacement", replacement_manifest),
            ):
                with self.subTest(target=target, field=label), self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "sync pending transaction path",
                ):
                    MODULE._manifest_model(current)

    def test_nested_pending_transaction_name_is_not_reserved(self) -> None:
        current = manifest("keep")
        current["links"][0]["target"] = (
            "nested/.personal-sync-pending-transaction.json"
        )

        MODULE._manifest_model(current)

    def test_manifest_rejects_portable_target_spelling_conflicts(self) -> None:
        active = manifest("one", "two")
        active["links"][0]["target"] = "Skills/Caf\N{LATIN SMALL LETTER E WITH ACUTE}"
        active["links"][1]["target"] = "skills/cafe\N{COMBINING ACUTE ACCENT}"

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "portable target spellings conflict",
        ):
            MODULE._manifest_model(active)

        removed_entry = removed("old", "remove-old", legacy=True)
        removed_entry["target"] = "skills/cafe\N{COMBINING ACUTE ACCENT}"
        current = manifest("keep", removed_links=[removed_entry])
        current["links"][0]["target"] = "Skills/Caf\N{LATIN SMALL LETTER E WITH ACUTE}"
        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "portable target spellings conflict",
        ):
            MODULE._manifest_model(current)

    def test_manifest_rejects_overlapping_targets(self) -> None:
        current = manifest("parent", "child")
        current["links"][0]["target"] = "skills"

        with self.assertRaisesRegex(MODULE.ValidationError, "must not overlap"):
            MODULE._manifest_model(current)

    def test_rejects_managed_target_hierarchy_changes(self) -> None:
        for previous_target, current_target in (
            ("skills", "skills/nested"),
            ("skills/nested", "skills"),
            ("Skills", "skills/nested"),
            ("skills/nested", "SKILLS"),
            (
                "Skills/Cafe\N{COMBINING ACUTE ACCENT}",
                "skills/Caf\N{LATIN SMALL LETTER E WITH ACUTE}/nested",
            ),
        ):
            with self.subTest(previous=previous_target, current=current_target):
                previous = manifest("previous")
                previous["links"][0]["target"] = previous_target
                removal = removed("previous", "remove-previous")
                removal["target"] = previous_target
                current = manifest("current", removed_links=[removal])
                current["links"][0]["target"] = current_target

                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "hierarchy changes are not supported",
                ):
                    MODULE.validate_manifest_change(previous, current)

    def test_manifest_history_rejects_active_target_hierarchy_changes(self) -> None:
        for historical_targets, active_target in (
            (("skills", "skills/a"), "skills/b"),
            (("skills/a",), "skills"),
        ):
            with self.subTest(
                historical_targets=historical_targets,
                active_target=active_target,
            ):
                removals = []
                for index, historical_target in enumerate(historical_targets):
                    entry = removed(f"old-{index}", f"remove-old-{index}")
                    entry["target"] = historical_target
                    entry["legacy"] = True
                    removals.append(entry)
                current = manifest("active", removed_links=removals)
                current["links"][0]["target"] = active_target

                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "hierarchy changes are not supported",
                ):
                    MODULE._manifest_model(current)

        allowed = manifest(
            "active",
            removed_links=[
                removed("same", "remove-same", legacy=True),
                removed("sibling", "remove-sibling", legacy=True),
            ],
        )
        allowed["links"][0]["target"] = "skills/a"
        allowed["removed_links"][0]["target"] = "skills/a"
        allowed["removed_links"][1]["target"] = "skills/b"

        MODULE._manifest_model(allowed)

    def test_manifest_declared_history_reserves_worst_case_records(self) -> None:
        current = manifest(
            "current",
            removed_links=[
                removed("old-one", "remove-old-one", legacy=True),
                removed("old-two", "remove-old-two", legacy=True),
            ],
        )
        with (
            mock.patch.object(MODULE, "MAX_PENDING_LINK_RECORDS", 3),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "declared history exceeds runtime transaction limit: 4 > 3",
            ),
        ):
            MODULE._manifest_model(current)

        current["removed_links"][1]["target"] = "skills/old-one"
        with mock.patch.object(MODULE, "MAX_PENDING_LINK_RECORDS", 3):
            MODULE._manifest_model(current)

    def test_hierarchy_change_check_normalizes_each_target_once(self) -> None:
        removed_targets = {
            f"item-{index:04d}/leaf" for index in range(0, 500, 2)
        }
        added_targets = {f"item-{index:04d}" for index in range(1, 500, 2)}
        original = MODULE._portable_target_key
        slice_count = 0

        class CountingKey(tuple):
            def __getitem__(self, key):
                nonlocal slice_count
                if isinstance(key, slice):
                    slice_count += 1
                value = super().__getitem__(key)
                return CountingKey(value) if isinstance(value, tuple) else value

        def counted_key(target: str) -> CountingKey:
            return CountingKey(original(target))

        with mock.patch.object(
            MODULE,
            "_portable_target_key",
            side_effect=counted_key,
        ) as portable_key:
            MODULE._validate_target_hierarchy_changes(
                removed_targets,
                added_targets,
            )

        self.assertEqual(
            portable_key.call_count,
            len(removed_targets) + len(added_targets),
        )
        self.assertGreater(slice_count, 0)
        self.assertLessEqual(
            slice_count,
            len(removed_targets) + len(added_targets) - 1,
        )

    def test_non_overlap_check_only_compares_adjacent_target_keys(self) -> None:
        targets = {
            (
                f"item-{index:04d}/leaf"
                if index % 2 == 0
                else f"item-{index:04d}"
            )
            for index in range(500)
        }
        original = MODULE._portable_target_key
        slice_count = 0

        class CountingKey(tuple):
            def __getitem__(self, key):
                nonlocal slice_count
                if isinstance(key, slice):
                    slice_count += 1
                value = super().__getitem__(key)
                return CountingKey(value) if isinstance(value, tuple) else value

        with mock.patch.object(
            MODULE,
            "_portable_target_key",
            side_effect=lambda target: CountingKey(original(target)),
        ) as portable_key:
            MODULE._validate_non_overlapping_targets(targets)

        self.assertEqual(portable_key.call_count, len(targets) * 2)
        self.assertGreater(slice_count, 0)
        self.assertLessEqual(slice_count, len(targets) - 1)

    def test_allows_portable_sibling_target_change(self) -> None:
        previous_target = "Skills/Cafe\N{COMBINING ACUTE ACCENT}/one"
        current_target = "skills/Caf\N{LATIN SMALL LETTER E WITH ACUTE}/two"
        previous = manifest("previous")
        previous["links"][0]["target"] = previous_target
        removal = removed("previous", "remove-previous")
        removal["target"] = previous_target
        current = manifest("current", removed_links=[removal])
        current["links"][0]["target"] = current_target

        MODULE.validate_manifest_change(previous, current)

    def test_historical_replacement_target_may_later_be_removed(self) -> None:
        rename = removed("retired", "rename-retired")
        rename["replacement_target"] = "skills/replacement"
        previous = manifest("keep", "replacement", removed_links=[rename])
        retirement = removed("replacement", "remove-replacement")
        retirement["retires_replacements"] = ["public:rename-retired"]
        current = manifest(
            "keep",
            removed_links=[rename, retirement],
        )

        MODULE.validate_manifest_change(previous, current)

    def test_skipped_release_removal_requires_all_historical_retirements(
        self,
    ) -> None:
        first_rename = removed("retired-one", "rename-retired-one")
        first_rename["replacement_target"] = "skills/replacement"
        second_rename = removed("retired-two", "rename-retired-two")
        second_rename["replacement_target"] = "skills/replacement"
        previous = manifest(
            "keep",
            "replacement",
            removed_links=[first_rename, second_rename],
        )
        previous["owner"] = "private"
        retirement = removed("replacement", "remove-replacement")
        retirement["retires_replacements"] = ["private:rename-retired-one"]
        current = manifest(
            "keep",
            removed_links=[first_rename, second_rename, retirement],
        )
        current["owner"] = "private"

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "private:rename-retired-two",
        ):
            MODULE.validate_manifest_change(previous, current)

        retirement["retires_replacements"].append("private:rename-retired-two")
        MODULE.validate_manifest_change(previous, current)

    def test_known_prior_history_preserves_replacement_retirement_requirements(
        self,
    ) -> None:
        rename = removed("retired", "rename-retired")
        rename["replacement_target"] = "skills/replacement"
        historical = manifest("keep", removed_links=[rename])
        historical["owner"] = "private"
        known_prior_removed = MODULE._manifest_model(
            historical,
            enforce_history_constraints=False,
        )["removed"]

        previous = manifest("keep", "replacement")
        previous["owner"] = "private"
        retirement = removed("replacement", "remove-replacement")
        current = manifest(
            "keep",
            removed_links=[rename, retirement],
        )
        current["owner"] = "private"

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "private:rename-retired",
        ):
            MODULE.validate_manifest_change(
                previous,
                current,
                known_prior_removed=known_prior_removed,
            )

        retirement["retires_replacements"] = ["private:rename-retired"]
        MODULE.validate_manifest_change(
            previous,
            current,
            known_prior_removed=known_prior_removed,
        )

    def test_active_replacement_identity_change_does_not_require_retirement(
        self,
    ) -> None:
        rename = removed("retired", "rename-retired")
        rename["replacement_target"] = "skills/replacement"
        previous = manifest("keep", "replacement", removed_links=[rename])
        identity_removal = removed("replacement", "replace-replacement-source")
        current = manifest(
            "keep",
            "replacement",
            removed_links=[rename, identity_removal],
        )
        current["links"][1]["source"] = "personal_codex/skills/replacement-v2"

        MODULE.validate_manifest_change(previous, current)

    def test_package_builder_preserves_removed_links_metadata(self) -> None:
        builder_path = REPO_ROOT / "scripts" / "build_personal_codex_package.py"
        spec = importlib.util.spec_from_file_location("build_personal_codex_package", builder_path)
        builder = importlib.util.module_from_spec(spec)
        assert spec is not None
        assert spec.loader is not None
        sys.modules[spec.name] = builder
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "personal_codex" / "skills" / "keep"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: keep\n---\n", encoding="utf-8")
            manifest_path = root / "personal_codex" / "public-sync-manifest.json"
            payload = manifest(
                "keep",
                removed_links=[removed("orphan", "remove-orphan", legacy=True)],
            )
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            staging = root / "staging"
            staging.mkdir()

            builder.stage_release(
                root,
                Path("personal_codex/public-sync-manifest.json"),
                staging,
            )

            packaged = json.loads(
                (staging / "personal_codex" / "sync-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(packaged["removed_links"], payload["removed_links"])

    def test_package_builder_strict_mode_rejects_untracked_source_file(self) -> None:
        builder_path = REPO_ROOT / "scripts" / "build_personal_codex_package.py"
        spec = importlib.util.spec_from_file_location(
            "build_personal_codex_package_strict",
            builder_path,
        )
        builder = importlib.util.module_from_spec(spec)
        assert spec is not None
        assert spec.loader is not None
        sys.modules[spec.name] = builder
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "personal_codex" / "skills" / "keep"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: keep\n---\n",
                encoding="utf-8",
            )
            manifest_path = root / "personal_codex" / "public-sync-manifest.json"
            manifest_path.write_text(json.dumps(manifest("keep")), encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "add",
                    "personal_codex/public-sync-manifest.json",
                    "personal_codex/skills/keep/SKILL.md",
                ],
                cwd=root,
                check=True,
            )
            (root / ".gitignore").write_text("local.txt\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--no-gpg-sign",
                    "-qm",
                    "Add package inputs",
                ],
                cwd=root,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            (source / "local.txt").write_text("local\n", encoding="utf-8")

            with self.assertRaisesRegex(builder.PackageError, "untracked files"):
                builder.build_package(
                    root,
                    Path("personal_codex/public-sync-manifest.json"),
                    root / "dist",
                    head,
                    require_clean_sources=True,
                )

            subprocess.run(
                ["git", "add", "-f", "personal_codex/skills/keep/local.txt"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--no-gpg-sign",
                    "-qm",
                    "Track ignored package input",
                ],
                cwd=root,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            archive, checksum = builder.build_package(
                root,
                Path("personal_codex/public-sync-manifest.json"),
                root / "dist",
                head,
                require_clean_sources=True,
            )
            self.assertTrue(archive.is_file())
            self.assertTrue(checksum.is_file())

    def test_package_builder_strict_mode_rejects_gitlink_source(self) -> None:
        builder_path = REPO_ROOT / "scripts" / "build_personal_codex_package.py"
        spec = importlib.util.spec_from_file_location(
            "build_personal_codex_package_gitlink",
            builder_path,
        )
        builder = importlib.util.module_from_spec(spec)
        assert spec is not None
        assert spec.loader is not None
        sys.modules[spec.name] = builder
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            module = root / "module"
            repo.mkdir()
            module.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "init", "-q"], cwd=module, check=True)
            (module / "SKILL.md").write_text(
                "---\nname: keep\n---\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "SKILL.md"], cwd=module, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--no-gpg-sign",
                    "-qm",
                    "Add skill",
                ],
                cwd=module,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    "-q",
                    str(module),
                    "personal_codex/skills/keep",
                ],
                cwd=repo,
                check=True,
            )
            manifest_path = repo / "personal_codex" / "public-sync-manifest.json"
            payload = manifest("keep")
            payload["links"][0] = {
                "source": "personal_codex/skills",
                "target": "skills",
                "kind": "directory",
            }
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            subprocess.run(
                ["git", "add", "personal_codex/public-sync-manifest.json"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--no-gpg-sign",
                    "-qm",
                    "Add package inputs",
                ],
                cwd=repo,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

            with self.assertRaisesRegex(builder.PackageError, "gitlink"):
                builder.build_package(
                    repo,
                    Path("personal_codex/public-sync-manifest.json"),
                    repo / "dist",
                    head,
                    require_clean_sources=True,
                )
            staging = repo / "staging"
            staging.mkdir()
            with self.assertRaisesRegex(builder.PackageError, "nested Git repository"):
                builder.stage_release(
                    repo,
                    Path("personal_codex/public-sync-manifest.json"),
                    staging,
                )


class ManifestGitTreeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="manifest-git-tree.")
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()
        self.manifest_path = Path("manifest.json")
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def write_manifest(self, payload: dict[str, object]) -> None:
        (self.repo / self.manifest_path).write_text(
            json.dumps(payload) + "\n",
            encoding="utf-8",
        )

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--no-gpg-sign",
            "-qm",
            message,
        )
        return self.git("rev-parse", "HEAD")

    def test_current_source_kind_is_bound_to_live_worktree(self) -> None:
        source = self.repo / "payload" / "source"
        source.mkdir(parents=True)
        (source / "child.txt").write_text("child\n", encoding="utf-8")
        current = manifest("keep")
        current["links"][0] = {
            "source": "payload/source",
            "target": "bin/example",
            "kind": "file",
        }
        self.write_manifest(current)
        self.commit("Add wrong-kind source")

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "file source is missing against live working tree",
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    self.manifest_path.as_posix(),
                ]
            )

    def test_current_skill_requires_live_skill_markdown(self) -> None:
        source = self.repo / "payload" / "skill"
        source.mkdir(parents=True)
        (source / "README.md").write_text("missing skill metadata\n", encoding="utf-8")
        current = manifest("keep")
        current["links"][0]["source"] = "payload/skill"
        self.write_manifest(current)
        self.commit("Add incomplete skill")

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "missing SKILL.md against live working tree",
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    self.manifest_path.as_posix(),
                ]
            )

    def test_dirty_manifest_can_reference_uncommitted_live_source(self) -> None:
        committed_skill = self.repo / "personal_codex" / "skills" / "keep"
        committed_skill.mkdir(parents=True)
        (committed_skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
        self.write_manifest(manifest("keep"))
        self.commit("Add committed manifest")

        live_source = self.repo / "payload" / "live.txt"
        live_source.parent.mkdir(parents=True)
        live_source.write_text("uncommitted\n", encoding="utf-8")
        dirty_manifest = manifest("keep")
        dirty_manifest["links"][0] = {
            "source": "payload/live.txt",
            "target": "bin/live",
            "kind": "file",
        }
        self.write_manifest(dirty_manifest)

        with mock.patch("builtins.print"):
            result = MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    self.manifest_path.as_posix(),
                ]
            )

        self.assertEqual(result, 0)
        self.assertIn("manifest.json", self.git("status", "--short"))
        self.assertIn("payload/", self.git("status", "--short"))

    def test_dirty_source_kind_is_validated_from_live_worktree(self) -> None:
        source = self.repo / "payload" / "source"
        source.parent.mkdir(parents=True)
        source.write_text("committed file\n", encoding="utf-8")
        current = manifest("keep")
        current["links"][0] = {
            "source": "payload/source",
            "target": "bin/example",
            "kind": "file",
        }
        self.write_manifest(current)
        self.commit("Add committed file source")

        source.unlink()
        source.mkdir()
        (source / "child.txt").write_text("dirty directory\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "file source is missing against live working tree",
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    self.manifest_path.as_posix(),
                ]
            )

    def test_live_manifest_and_sources_reject_descendant_snapshot_mix(self) -> None:
        active = self.repo / "bundle"
        replacement = self.repo / "bundle-next"
        active.mkdir()
        replacement.mkdir()
        current = manifest("keep")
        current["links"][0] = {
            "source": "bundle/source.txt",
            "target": "bin/example",
            "kind": "file",
        }
        encoded_manifest = json.dumps(current) + "\n"
        for bundle in (active, replacement):
            (bundle / "manifest.json").write_text(
                encoded_manifest,
                encoding="utf-8",
            )
            (bundle / "source.txt").write_text("source\n", encoding="utf-8")

        real_parse = MODULE._parse_manifest_bytes
        swapped = False

        def swap_descendant_after_manifest_read(
            payload: bytes,
            description: str,
        ) -> dict[str, object]:
            nonlocal swapped
            parsed = real_parse(payload, description)
            if not swapped and description.startswith("manifest "):
                swapped = True
                active.rename(self.repo / "bundle-old")
                replacement.rename(active)
            return parsed

        with (
            mock.patch.object(
                MODULE,
                "_parse_manifest_bytes",
                side_effect=swap_descendant_after_manifest_read,
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "live manifest changed while validating its source paths",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    "bundle/manifest.json",
                ]
            )

        self.assertTrue(swapped)

    def test_live_source_binding_is_rechecked_after_validation(self) -> None:
        active = self.repo / "payload"
        replacement = self.repo / "payload-next"
        active.mkdir()
        replacement.mkdir()
        for directory in (active, replacement):
            (directory / "source.txt").write_text("source\n", encoding="utf-8")
        current = manifest("keep")
        current["links"][0] = {
            "source": "payload/source.txt",
            "target": "bin/example",
            "kind": "file",
        }
        self.write_manifest(current)

        real_resolve = MODULE._LiveWorktreePathKind.__call__
        swapped = False

        def swap_descendant_after_source_lookup(
            resolver: object,
            raw_path: str,
        ) -> str | None:
            nonlocal swapped
            kind = real_resolve(resolver, raw_path)
            if not swapped and raw_path == "payload/source.txt":
                swapped = True
                active.rename(self.repo / "payload-old")
                replacement.rename(active)
            return kind

        with (
            mock.patch.object(
                MODULE._LiveWorktreePathKind,
                "__call__",
                side_effect=swap_descendant_after_source_lookup,
                autospec=True,
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "manifest source path changed between live validation passes",
            ),
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    self.manifest_path.as_posix(),
                ]
            )

        self.assertTrue(swapped)

    def test_live_source_rejects_symlink_leaf_and_ancestor(self) -> None:
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("outside\n", encoding="utf-8")
        payload = self.repo / "payload"
        payload.mkdir()
        (payload / "leaf-link").symlink_to(outside / "file.txt")
        (payload / "ancestor-link").symlink_to(outside, target_is_directory=True)

        cases = (
            ("payload/leaf-link", "is a symlink"),
            ("payload/ancestor-link/file.txt", "contains a symlink component"),
        )
        for source, message in cases:
            current = manifest("keep")
            current["links"][0] = {
                "source": source,
                "target": "bin/example",
                "kind": "file",
            }
            self.write_manifest(current)
            with self.subTest(source=source), self.assertRaisesRegex(
                MODULE.ValidationError,
                message,
            ):
                MODULE.main(
                    [
                        "--repo-root",
                        str(self.repo),
                        "--manifest",
                        self.manifest_path.as_posix(),
                    ]
                )

    def test_base_source_kind_is_bound_to_resolved_commit(self) -> None:
        source = self.repo / "payload" / "source"
        source.mkdir(parents=True)
        (source / "child.txt").write_text("child\n", encoding="utf-8")
        previous = manifest("keep")
        previous["links"][0] = {
            "source": "payload/source",
            "target": "bin/example",
            "kind": "file",
        }
        self.write_manifest(previous)
        base_commit = self.commit("Add invalid baseline source")

        current_source = self.repo / "payload" / "current.txt"
        current_source.write_text("current\n", encoding="utf-8")
        current = manifest("keep")
        current["links"][0] = {
            "source": "payload/current.txt",
            "target": "bin/example",
            "kind": "file",
        }
        self.write_manifest(current)
        self.commit("Repair current source")

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            f"file source is missing against base commit {base_commit}",
        ):
            MODULE.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--manifest",
                    self.manifest_path.as_posix(),
                    "--base-ref",
                    base_commit,
                ]
            )

    def test_git_tree_path_kind_inventory_is_single_and_bounded(self) -> None:
        skill = self.repo / "payload" / "skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
        self.write_manifest(manifest("keep"))
        head = self.commit("Add tree inventory")

        with mock.patch.object(
            MODULE,
            "_bounded_git_output",
            wraps=MODULE._bounded_git_output,
        ) as bounded_git_output:
            path_kind = MODULE._git_tree_path_kind_resolver(self.repo, head)
            self.assertEqual(path_kind("payload"), "directory")
            self.assertEqual(path_kind("payload/skill"), "directory")
            self.assertEqual(path_kind("payload/skill/SKILL.md"), "file")
            self.assertIsNone(path_kind("payload/missing"))

        tree_calls = [
            call
            for call in bounded_git_output.call_args_list
            if call.args[1][1] == "ls-tree"
        ]
        self.assertEqual(len(tree_calls), 1)
        self.assertEqual(
            tree_calls[0].kwargs["stdout_limit"],
            MODULE.MAX_GIT_TREE_LISTING_BYTES,
        )

    def test_git_tree_path_kind_inventory_rejects_exact_duplicate_records(
        self,
    ) -> None:
        commit = "a" * 40
        record = (
            b"100644 blob "
            + b"b" * 40
            + b"\tpayload/file.txt\0"
        )
        result = subprocess.CompletedProcess(
            args=["git", "ls-tree"],
            returncode=0,
            stdout=record + record,
            stderr=b"",
        )

        with mock.patch.object(
            MODULE,
            "_bounded_git_output",
            return_value=result,
        ), self.assertRaisesRegex(
            MODULE.ValidationError,
            "duplicate tree path-kind entry",
        ):
            MODULE._git_tree_path_kind_resolver(self.repo, commit)


class ManifestSizeBoundTests(unittest.TestCase):
    def test_rejects_oversized_current_manifest_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = Path("manifest.json")
            (root / manifest_path).write_bytes(
                b" " * (MODULE.MAX_RELEASE_MANIFEST_BYTES + 1)
            )

            with self.assertRaisesRegex(MODULE.ValidationError, "exceeds"):
                MODULE._load_json(root, manifest_path)

    def test_rejects_oversized_baseline_manifest_before_reading_blob(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = root / "--manifest.json"
            manifest_path.write_bytes(b" " * (MODULE.MAX_RELEASE_MANIFEST_BYTES + 1))
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "--", "--manifest.json"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--no-gpg-sign",
                    "-qm",
                    "Add oversized manifest",
                ],
                cwd=root,
                check=True,
            )

            with self.assertRaisesRegex(MODULE.ValidationError, "exceeds"):
                MODULE._manifest_at_ref(root, "HEAD", Path("--manifest.json"))

    def test_rejects_compact_manifest_with_oversized_pretty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = manifest("keep")
            padding_count = MODULE.MAX_RELEASE_MANIFEST_BYTES // 7 + 1
            payload["padding"] = [0] * padding_count
            compact = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.assertLess(len(compact), MODULE.MAX_RELEASE_MANIFEST_BYTES)
            (root / "manifest.json").write_bytes(compact)

            with self.assertRaisesRegex(
                MODULE.ValidationError,
                "serialized release manifest exceeds",
            ):
                MODULE.main(
                    [
                        "--repo-root",
                        str(root),
                        "--manifest",
                        "manifest.json",
                    ]
                )


class ManifestSerializationSafetyTests(unittest.TestCase):
    def test_rejects_single_json_token_before_encoder_materialization(self) -> None:
        payload = manifest("keep")
        payload["padding"] = "\N{PILE OF POO}" * (
            MODULE.MAX_RELEASE_MANIFEST_BYTES // 12 + 1
        )

        with (
            mock.patch.object(
                MODULE.json.JSONEncoder,
                "iterencode",
                side_effect=AssertionError("encoder must not run"),
            ),
            self.assertRaisesRegex(
                MODULE.ValidationError,
                "serialized release manifest exceeds",
            ),
        ):
            MODULE._release_manifest_payload(payload)

    def test_deep_json_is_reported_as_validation_error(self) -> None:
        nested: object = None
        for _index in range(sys.getrecursionlimit() + 10):
            nested = [nested]
        payload = manifest("keep")
        payload["nested"] = nested

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "failed to serialize release manifest",
        ):
            MODULE._release_manifest_payload(payload)

    def test_deep_raw_json_is_reported_as_validation_error(self) -> None:
        depth = sys.getrecursionlimit() + 10
        payload = (
            b'{"version":1,"nested":'
            + b"[" * depth
            + b"0"
            + b"]" * depth
            + b"}"
        )

        with self.assertRaisesRegex(
            MODULE.ValidationError,
            "invalid JSON|failed to serialize release manifest",
        ):
            MODULE._parse_manifest_bytes(payload, "manifest")

    def test_oversized_integer_is_reported_as_validation_error(self) -> None:
        payload = (
            b'{"version":'
            + b"9" * (MODULE.MAX_JSON_INTEGER_DIGITS + 1)
            + b"}"
        )

        with self.assertRaisesRegex(MODULE.ValidationError, "invalid JSON"):
            MODULE._parse_manifest_bytes(payload, "manifest")


class ManifestDescriptorSafetyTests(unittest.TestCase):
    @staticmethod
    def _write_valid_manifest(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest("keep")), encoding="utf-8")

    def test_rejects_unsafe_manifest_path_before_repo_access(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing_root = Path(raw) / "missing-validator-repo"
            for unsafe in ("../manifest.json", "/tmp/manifest.json", "."):
                with self.subTest(path=unsafe), self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "safe relative path",
                ):
                    MODULE.main(
                        [
                            "--repo-root",
                            str(missing_root),
                            "--manifest",
                            unsafe,
                        ]
                    )

    def test_rejects_symlink_manifest_ancestor_and_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            root = workspace / "repo"
            outside = workspace / "outside"
            root.mkdir()
            outside.mkdir()
            self._write_valid_manifest(outside / "manifest.json")
            (root / "linked").symlink_to(outside, target_is_directory=True)
            (root / "leaf.json").symlink_to(outside / "manifest.json")

            for relative in ("linked/manifest.json", "leaf.json"):
                with self.subTest(path=relative), self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "failed to read manifest safely",
                ):
                    MODULE.main(
                        [
                            "--repo-root",
                            str(root),
                            "--manifest",
                            relative,
                        ]
                    )

    def test_rejects_fifo_manifest_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.mkfifo(root / "manifest.json")

            with self.assertRaisesRegex(
                MODULE.ValidationError,
                "not a regular file",
            ):
                MODULE.main(
                    [
                        "--repo-root",
                        str(root),
                        "--manifest",
                        "manifest.json",
                    ]
                )

    def test_rejects_leaf_replacement_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = root / "manifest.json"
            self._write_valid_manifest(manifest_path)
            replacement = root / "replacement.json"
            self._write_valid_manifest(replacement)
            real_read = MODULE.os.read
            swapped = False

            def replacing_read(file_descriptor: int, size: int) -> bytes:
                nonlocal swapped
                chunk = real_read(file_descriptor, size)
                if not swapped:
                    swapped = True
                    os.replace(replacement, manifest_path)
                return chunk

            with (
                mock.patch.object(MODULE.os, "read", side_effect=replacing_read),
                self.assertRaisesRegex(MODULE.ValidationError, "changed while being read"),
            ):
                MODULE._load_json(root, Path("manifest.json"))

    def test_rejects_ancestor_replacement_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            nested = root / "nested"
            self._write_valid_manifest(nested / "manifest.json")
            moved = root / "moved"
            real_read = MODULE.os.read
            swapped = False

            def replacing_read(file_descriptor: int, size: int) -> bytes:
                nonlocal swapped
                chunk = real_read(file_descriptor, size)
                if not swapped:
                    swapped = True
                    nested.rename(moved)
                    self._write_valid_manifest(nested / "manifest.json")
                return chunk

            with (
                mock.patch.object(MODULE.os, "read", side_effect=replacing_read),
                self.assertRaisesRegex(MODULE.ValidationError, "ancestor changed"),
            ):
                MODULE._load_json(root, Path("nested/manifest.json"))


class ManifestPathEncodingTests(unittest.TestCase):
    def test_rejects_json_nul_and_lone_surrogate_in_manifest_paths(self) -> None:
        payloads = {
            "source-nul": rb'{"version":1,"links":[{"source":"personal_codex/skills\u0000/bad","target":"skills/example","kind":"skill"}]}',
            "source-surrogate": rb'{"version":1,"links":[{"source":"personal_codex/skills/\ud800","target":"skills/example","kind":"skill"}]}',
            "target-nul": rb'{"version":1,"links":[{"source":"personal_codex/skills/example","target":"skills\u0000/example","kind":"skill"}]}',
            "target-surrogate": rb'{"version":1,"links":[{"source":"personal_codex/skills/example","target":"skills/\ud800","kind":"skill"}]}',
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                data = json.loads(payload.decode("utf-8"))
                with self.assertRaisesRegex(
                    MODULE.ValidationError,
                    "embedded NUL|valid UTF-8",
                ):
                    MODULE._manifest_model(data)

    def test_rejects_escaped_surrogate_outside_paths(self) -> None:
        data = json.loads(
            rb'{"version":1,"unknown":"\ud800","links":[{"source":"personal_codex/skills/example","target":"skills/example","kind":"skill"}]}'.decode(
                "utf-8"
            )
        )

        with self.assertRaisesRegex(MODULE.ValidationError, "not valid UTF-8"):
            MODULE._manifest_model(data)

    def test_load_json_translates_embedded_nul_value_error(self) -> None:
        with self.assertRaisesRegex(MODULE.ValidationError, "embedded NUL"):
            MODULE._load_json(Path.cwd(), Path("bad\0manifest.json"))

    def test_cli_path_arguments_reject_lone_surrogate(self) -> None:
        with self.assertRaisesRegex(MODULE.ValidationError, "valid UTF-8"):
            MODULE._path_argument("bad\ud800manifest.json", "--manifest")


if __name__ == "__main__":
    unittest.main()
