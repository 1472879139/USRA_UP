"""发布包哈希、路径限制及已有文件保留；不运行安装器或修改服务。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/mcp" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.build = module("build_release").build
        self.unpack = module("install_gpu").unpack

    def test_release_only_contains_declared_code_and_lock(self):
        package = self.root / "release.zip"
        result = self.build(package)
        target, manifest = self.unpack(package, result["sha256"], self.root / "installed")
        self.assertEqual(result["release_id"], manifest["release_id"])
        self.assertTrue((target / "ursa_mcp/server.py").exists())
        self.assertTrue((target / "ursa_mcp/__init__.py").exists())
        self.assertTrue((target / "ursa_mcp/v2.py").exists())
        self.assertTrue((target / "requirements-mcp.lock").exists())
        self.assertFalse(any("local.json" in name or name.endswith((".tif", ".pt", ".key")) for name in manifest["files"]))
        with self.assertRaises(FileExistsError):
            self.build(package)

    def test_wrong_bundle_hash_is_rejected_before_writes(self):
        package = self.root / "release.zip"
        self.build(package)
        target = self.root / "installed"
        with self.assertRaises(ValueError):
            self.unpack(package, "0" * 64, target)
        self.assertFalse(target.exists())

    def test_manifest_path_escape_is_rejected(self):
        package = self.root / "unsafe.zip"
        content = b"untrusted-fixture"
        manifest = {"service": "ursa_mcp", "release_id": "a" * 16,
                    "files": {"../outside.py": hashlib.sha256(content).hexdigest()}}
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../outside.py", content)
            archive.writestr("release-manifest.json", json.dumps(manifest))
        with self.assertRaises(ValueError):
            self.unpack(package, hashlib.sha256(package.read_bytes()).hexdigest(), self.root / "installed")
        self.assertFalse((self.root / "outside.py").exists())

    def test_existing_modified_release_is_not_overwritten(self):
        package = self.root / "release.zip"
        result = self.build(package)
        target, _manifest = self.unpack(package, result["sha256"], self.root / "installed")
        changed = target / "ursa_mcp/server.py"
        changed.write_text("保留已有修改", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.unpack(package, result["sha256"], self.root / "installed")
        self.assertEqual(changed.read_text(encoding="utf-8"), "保留已有修改")


if __name__ == "__main__":
    unittest.main()
