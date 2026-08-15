import unittest


class PackageBoundaryTests(unittest.TestCase):
    def test_package_exports_local_kernel_initializer(self) -> None:
        import kernel

        self.assertTrue(callable(kernel.initialize))


if __name__ == "__main__":
    unittest.main()
