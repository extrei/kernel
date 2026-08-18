import unittest


class PackageBoundaryTests(unittest.TestCase):
    def test_package_exports_the_local_step_ledger_api(self) -> None:
        import kernel

        self.assertTrue(callable(kernel.apply_patch))
        self.assertTrue(callable(kernel.audit_schema))
        self.assertTrue(callable(kernel.audit_views))
        self.assertTrue(callable(kernel.collection))
        self.assertTrue(callable(kernel.entries))
        self.assertTrue(callable(kernel.derive_view))
        self.assertTrue(callable(kernel.initialize))
        self.assertTrue(callable(kernel.record_step))
        self.assertTrue(callable(kernel.schema))
        self.assertTrue(callable(kernel.set_schema))
        self.assertTrue(callable(kernel.state))
        self.assertTrue(callable(kernel.validate))
        self.assertTrue(callable(kernel.view))


if __name__ == "__main__":
    unittest.main()
