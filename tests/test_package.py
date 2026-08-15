import unittest


class PackageBoundaryTests(unittest.TestCase):
    def test_package_exports_the_local_step_ledger_api(self) -> None:
        import kernel

        self.assertTrue(callable(kernel.entries))
        self.assertTrue(callable(kernel.initialize))
        self.assertTrue(callable(kernel.record_step))


if __name__ == "__main__":
    unittest.main()
