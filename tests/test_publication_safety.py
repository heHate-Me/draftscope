from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_publication_safety.py"
_SPEC = importlib.util.spec_from_file_location("check_publication_safety", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
publication_safety = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = publication_safety
_SPEC.loader.exec_module(publication_safety)


class PublicationPathTests(unittest.TestCase):
    def reasons(self, path: str) -> set[str]:
        return {finding.reason for finding in publication_safety.check_path_name(path)}

    def test_allows_public_templates_and_source(self) -> None:
        self.assertEqual(self.reasons("draftscope.toml.example"), set())
        self.assertEqual(self.reasons("src/draftscope/model.py"), set())

    def test_rejects_runtime_and_local_config_paths(self) -> None:
        self.assertTrue(self.reasons("draftscope.toml"))
        self.assertTrue(self.reasons("data/players.csv"))
        self.assertTrue(self.reasons(".draftscope-cache/response.json"))
        self.assertTrue(self.reasons(".env.local"))

    def test_rejects_transaction_cache_and_crash_paths(self) -> None:
        self.assertTrue(self.reasons(".data.draftscope-update-123/state.json"))
        self.assertTrue(self.reasons(".data.draftscope-transaction-123.json"))
        self.assertTrue(self.reasons("pkg/__pycache__/model.pyc"))
        self.assertTrue(self.reasons("crashes/process.stackdump"))

    def test_rejects_likely_private_key_files(self) -> None:
        self.assertTrue(self.reasons("id_ed25519"))
        self.assertTrue(self.reasons("credentials/client.pem"))


class PublicationContentTests(unittest.TestCase):
    def reasons(self, data: bytes) -> set[str]:
        return {
            finding.reason
            for finding in publication_safety.check_file_content("sample.txt", data)
        }

    def test_allows_portable_and_explicitly_synthetic_paths(self) -> None:
        self.assertEqual(self.reasons(b"data/players.csv\n"), set())
        synthetic = b"/Users/" + b"example/project/file.csv\n"
        self.assertEqual(self.reasons(synthetic), set())

    def test_rejects_private_key_material(self) -> None:
        private_key = b"-----BEGIN " + b"PRIVATE KEY-----\nnot-a-real-key\n"
        self.assertIn("private-key material", self.reasons(private_key))

    def test_rejects_credential_shaped_tokens(self) -> None:
        github_token = b"ghp_" + (b"A" * 40)
        aws_key = b"AK" + b"IA" + (b"A" * 16)
        reasons = self.reasons(github_token + b"\n" + aws_key)
        self.assertIn("GitHub credential-shaped token", reasons)
        self.assertIn("AWS access-key-shaped token", reasons)

    def test_rejects_machine_local_paths(self) -> None:
        mac_home = b"/Users/" + b"alice/private/file.csv"
        linux_home = b"/home/" + b"alice/private/file.csv"
        windows_home = b"C:\\Users\\" + b"alice\\private\\file.csv"
        root_home = b"/" + b"root/private/file.csv"
        mac_temp = b"/private/" + b"var/folders/ab/cd/T/file.csv"
        for value in (mac_home, linux_home, windows_home, root_home, mac_temp):
            with self.subTest(value=value):
                self.assertTrue(self.reasons(value))


if __name__ == "__main__":
    unittest.main()
