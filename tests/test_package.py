import unittest


class PackageBoundaryTests(unittest.TestCase):
    def test_package_exports_the_local_step_ledger_api(self) -> None:
        import kernel

        self.assertTrue(callable(kernel.apply_patch))
        self.assertTrue(callable(kernel.blueprint))
        self.assertTrue(callable(kernel.check_blueprint))
        self.assertTrue(callable(kernel.circuit))
        self.assertTrue(callable(kernel.audit_schema))
        self.assertTrue(callable(kernel.audit_views))
        self.assertTrue(callable(kernel.collection))
        self.assertTrue(callable(kernel.entries))
        self.assertTrue(callable(kernel.events))
        self.assertTrue(callable(kernel.derive_view))
        self.assertTrue(callable(kernel.initialize))
        self.assertTrue(callable(kernel.meta_schema))
        self.assertTrue(callable(kernel.record_failure))
        self.assertTrue(callable(kernel.record_step))
        self.assertTrue(callable(kernel.schedule))
        self.assertTrue(callable(kernel.schema))
        self.assertTrue(callable(kernel.set_blueprint))
        self.assertTrue(callable(kernel.state))
        self.assertTrue(callable(kernel.validate))
        self.assertTrue(callable(kernel.view))
        self.assertFalse(hasattr(kernel, "set_contracts"))
        self.assertFalse(hasattr(kernel, "set_schema"))


if __name__ == "__main__":
    unittest.main()
