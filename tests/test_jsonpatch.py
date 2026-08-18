from copy import deepcopy
import unittest

from kernel.jsonpatch import PatchError, apply_patch, touched_paths


class JsonPatchTests(unittest.TestCase):
    def test_touched_paths_validates_the_whole_patch_without_state(self) -> None:
        patch = [
            {"op": "test", "path": "/plan/status", "value": "draft"},
            {"op": "add", "path": "/claims/-", "value": {}},
            {"op": "remove", "path": "/obsolete"},
        ]

        self.assertEqual(
            touched_paths(patch),
            [
                ("test", "/plan/status"),
                ("add", "/claims/-"),
                ("remove", "/obsolete"),
            ],
        )

        with self.assertRaisesRegex(PatchError, "operation 1"):
            touched_paths(
                [
                    {"op": "add", "path": "/valid", "value": 1},
                    {"op": "replace", "path": "invalid", "value": 2},
                ]
            )

    def test_add_replace_test_and_pointer_escapes_apply_to_a_copy(self) -> None:
        original = {
            "profile": {"name": "Ada"},
            "tags": ["one"],
            "a/b": {"~key": 1},
        }
        before = deepcopy(original)

        result = apply_patch(
            original,
            [
                {"op": "test", "path": "/profile/name", "value": "Ada"},
                {"op": "replace", "path": "/profile/name", "value": "Grace"},
                {"op": "add", "path": "/profile/active", "value": True},
                {"op": "add", "path": "/tags/-", "value": "two"},
                {"op": "replace", "path": "/a~1b/~0key", "value": 2},
            ],
        )

        self.assertEqual(original, before)
        self.assertEqual(
            result,
            {
                "profile": {"name": "Grace", "active": True},
                "tags": ["one", "two"],
                "a/b": {"~key": 2},
            },
        )

    def test_array_add_inserts_and_root_replace_is_supported(self) -> None:
        self.assertEqual(
            apply_patch(
                {"items": ["a", "c"]},
                [{"op": "add", "path": "/items/1", "value": "b"}],
            ),
            {"items": ["a", "b", "c"]},
        )
        self.assertEqual(
            apply_patch({"old": True}, [{"op": "replace", "path": "", "value": {}}]),
            {},
        )

    def test_remove_requires_explicit_privilege(self) -> None:
        patch = [{"op": "remove", "path": "/obsolete"}]
        with self.assertRaisesRegex(PatchError, "remove is not allowed"):
            apply_patch({"obsolete": True}, patch)
        self.assertEqual(
            apply_patch({"obsolete": True}, patch, allow_remove=True),
            {},
        )

    def test_move_copy_and_failed_test_are_typed_errors(self) -> None:
        for operation in (
            {"op": "move", "from": "/a", "path": "/b"},
            {"op": "copy", "from": "/a", "path": "/b"},
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(PatchError):
                    apply_patch({"a": 1}, [operation])

        with self.assertRaisesRegex(PatchError, "test failed"):
            apply_patch({"a": 1}, [{"op": "test", "path": "/a", "value": 2}])

    def test_failure_never_mutates_the_input(self) -> None:
        original = {"items": [1]}
        with self.assertRaises(PatchError):
            apply_patch(
                original,
                [
                    {"op": "add", "path": "/items/-", "value": 2},
                    {"op": "replace", "path": "/missing", "value": 3},
                ],
            )
        self.assertEqual(original, {"items": [1]})

    def test_invalid_pointer_and_array_index_are_rejected(self) -> None:
        for path in ("not-a-pointer", "/items/01", "/items/~2"):
            with self.subTest(path=path):
                with self.assertRaises(PatchError):
                    apply_patch(
                        {"items": [1]},
                        [{"op": "replace", "path": path, "value": 2}],
                    )

    def test_copy_failures_are_typed(self) -> None:
        class Uncopyable:
            def __deepcopy__(self, memo):
                raise RuntimeError("cannot copy")

        with self.assertRaisesRegex(PatchError, "document cannot be copied"):
            apply_patch(Uncopyable(), [])
        with self.assertRaisesRegex(PatchError, "operation 0 failed"):
            apply_patch({}, [{"op": "add", "path": "/value", "value": Uncopyable()}])


if __name__ == "__main__":
    unittest.main()
