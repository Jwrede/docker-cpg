"""Integration tests against a real Docker daemon.

The test image is imported from a tar file: it has no shell and no tar, and
the containers are never started.
"""

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout

import docker_cpg as m


def docker_available():
    if os.environ.get("DOCKER_CPG_REQUIRE_DOCKER"):
        return True  # CI: fail instead of skipping
    if shutil.which("docker") is None:
        return False
    return subprocess.call(["docker", "info"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30) == 0


def add(tf, name, data=None, mode=0o644, kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.mtime = 1700000000
    info.linkname = link
    if data is not None:
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    else:
        tf.addfile(info)


def rootfs_tar():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for d in ["var", "var/log", "var/log/app", "var/log/.cache", "srv",
                  "srv/a", "srv/b", "data"]:
            add(tf, d, kind=tarfile.DIRTYPE, mode=0o755)
        add(tf, "var/log/a.log", b"a\n")
        add(tf, "var/log/b.log", b"b\n")
        add(tf, "var/log/notes.txt", b"n\n")
        add(tf, "var/log/.hidden.log", b"h\n")
        add(tf, "var/log/app/c.log", b"c\n")
        add(tf, "var/log/app/skip.tmp", b"t\n")
        add(tf, "var/log/.cache/d.log", b"d\n")
        add(tf, "var/log/link.log", kind=tarfile.SYMTYPE, link="/var/log/a.log")
        add(tf, "srv/a/x.log", b"from a\n")
        add(tf, "srv/b/x.log", b"from b\n")
        add(tf, "data/ro", kind=tarfile.DIRTYPE, mode=0o555)
        add(tf, "data/ro/inside.txt", b"ro\n", mode=0o444)
        add(tf, "data/orig.bin", b"same inode\n")
        add(tf, "data/hard.bin", kind=tarfile.LNKTYPE, link="data/orig.bin")
        add(tf, "upload", kind=tarfile.DIRTYPE, mode=0o755)
    return buf.getvalue()


@unittest.skipUnless(docker_available(), "docker daemon not available")
class DockerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = "docker-cpg-test:%s" % uuid.uuid4().hex[:8]
        subprocess.run(["docker", "import", "-", cls.image], input=rootfs_tar(),
                       check=True, stdout=subprocess.DEVNULL, timeout=120)
        cls.containers = []

    @classmethod
    def tearDownClass(cls):
        if cls.containers:
            subprocess.call(["docker", "rm", "-f"] + cls.containers,
                            stdout=subprocess.DEVNULL, timeout=60)
        subprocess.call(["docker", "rmi", "-f", cls.image], stdout=subprocess.DEVNULL,
                        timeout=60)

    def setUp(self):
        name = "docker-cpg-test-%s" % uuid.uuid4().hex[:8]
        subprocess.run(["docker", "create", "--name", name, self.image, "/none"],
                       check=True, stdout=subprocess.DEVNULL, timeout=60)
        self.containers.append(name)
        self.c = name
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self.cleanup_tmp)

    def cleanup_tmp(self):
        for root, dirs, _ in os.walk(self.tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
        shutil.rmtree(self.tmp)

    def cpg(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = m.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def tree(self, path):
        result = []
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                result.append(os.path.relpath(os.path.join(root, n), path))
        return sorted(result)

    def read(self, *parts):
        with open(os.path.join(self.tmp, *parts)) as f:
            return f.read()

    def test_star_copies_matching_files_and_keeps_symlinks(self):
        rc, _, err = self.cpg("%s:/var/log/*.log" % self.c, self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(self.tmp), ["a.log", "b.log", "link.log"])
        self.assertEqual(os.readlink(os.path.join(self.tmp, "link.log")), "/var/log/a.log")
        self.assertEqual(self.read("b.log"), "b\n")

    def test_double_star_recurses_but_skips_hidden(self):
        rc, _, err = self.cpg("%s:/var/**/*.log" % self.c, self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(self.tmp), ["a.log", "b.log", "c.log", "link.log"])

    def test_matched_directory_is_copied_recursively(self):
        rc, _, err = self.cpg("%s:/var/log/ap*" % self.c, self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(self.tmp), ["app", "app/c.log", "app/skip.tmp"])

    def test_exclude(self):
        rc, _, err = self.cpg("-e", "*.tmp", "%s:/var/log/a*" % self.c, self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(self.tmp), ["a.log", "app", "app/c.log"])

    def test_exclude_applies_to_directories_between_base_and_match(self):
        rc, _, err = self.cpg("-e", "app", "%s:/var/**/*.log" % self.c, self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(self.tmp), ["a.log", "b.log", "link.log"])
        dest = os.path.join(self.tmp, "p")
        rc, _, err = self.cpg("-e", "log/app/*.log", "%s:/var/**/*.log" % self.c, dest)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(dest), ["a.log", "b.log", "link.log"])

    def test_name_collision_needs_parents(self):
        rc, _, err = self.cpg("%s:/srv/*/x.log" % self.c, self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertIn("already copied to the same name", err)
        self.assertEqual(self.read("x.log"), "from a\n")

        dest = os.path.join(self.tmp, "p")
        rc, _, err = self.cpg("--parents", "%s:/srv/*/x.log" % self.c, dest)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read("p", "srv", "b", "x.log"), "from b\n")

    def test_several_sources(self):
        rc, _, err = self.cpg("%s:/var/log/a.log" % self.c, "%s:/srv/a/*.log" % self.c,
                              self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(self.tmp), ["a.log", "x.log"])

    def test_no_match_exits_1(self):
        rc, _, err = self.cpg("%s:/var/log/*.nope" % self.c, self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("no match", err)
        rc, _, _ = self.cpg("%s:/missing/*" % self.c, self.tmp)
        self.assertEqual(rc, 1)

    def test_read_only_directory_and_hard_link(self):
        rc, _, err = self.cpg("%s:/data/*" % self.c, self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read("ro", "inside.txt"), "ro\n")
        self.assertEqual(os.stat(os.path.join(self.tmp, "ro")).st_mode & 0o777, 0o555)
        a = os.stat(os.path.join(self.tmp, "orig.bin"))
        b = os.stat(os.path.join(self.tmp, "hard.bin"))
        self.assertEqual(a.st_ino, b.st_ino)

    def test_existing_symlink_in_destination_is_replaced_not_followed(self):
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w") as f:
            f.write("keep\n")
        dest = os.path.join(self.tmp, "dest")
        os.mkdir(dest)
        os.symlink(outside, os.path.join(dest, "a.log"))
        rc, _, err = self.cpg("%s:/var/log/a.*" % self.c, dest)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read("outside.txt"), "keep\n")
        self.assertFalse(os.path.islink(os.path.join(dest, "a.log")))
        self.assertEqual(self.read("dest", "a.log"), "a\n")

    def test_dry_run_writes_nothing(self):
        dest = os.path.join(self.tmp, "dry")
        rc, out, err = self.cpg("-n", "%s:/var/log/*.log" % self.c, dest)
        self.assertEqual(rc, 0, err)
        self.assertIn("/var/log/a.log -> ", out)
        self.assertFalse(os.path.exists(dest))

    def test_stdout_tar(self):
        out = subprocess.run(
            [sys.executable, "-m", "docker_cpg", "-q", "%s:/var/log/*.txt" % self.c, "-"],
            stdout=subprocess.PIPE, check=True, timeout=120,
            env=dict(os.environ, PYTHONPATH=os.pathsep.join(
                [os.path.join(os.path.dirname(__file__), "..", "src")]))).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as tf:
            self.assertEqual(tf.getnames(), ["notes.txt"])
            self.assertEqual(tf.extractfile("notes.txt").read(), b"n\n")

    def test_copy_in_several_sources_with_exclude(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(src, "proj", "node_modules", "x"))
        os.makedirs(os.path.join(src, "proj", "lib"))
        for rel, text in [("proj/lib/m.py", "m"), ("proj/node_modules/x/i.js", "i"),
                          ("one.txt", "1"), ("two.txt", "2")]:
            with open(os.path.join(src, rel), "w") as f:
                f.write(text)
        rc, _, err = self.cpg("-e", "node_modules", os.path.join(src, "proj"),
                              os.path.join(src, "*.txt"), "%s:/upload" % self.c)
        self.assertEqual(rc, 0, err)

        back = os.path.join(self.tmp, "back")
        rc, _, err = self.cpg("%s:/upload/*" % self.c, back)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(back),
                         ["one.txt", "proj", "proj/lib", "proj/lib/m.py", "two.txt"])
        listing = subprocess.run(["docker", "cp", "%s:/upload/one.txt" % self.c, "-"],
                                 stdout=subprocess.PIPE, check=True, timeout=60).stdout
        with tarfile.open(fileobj=io.BytesIO(listing)) as tf:
            self.assertEqual(tf.getmember("one.txt").uid, 0)

    def test_single_plain_source_is_passed_to_docker_cp(self):
        dest = os.path.join(self.tmp, "renamed.log")
        rc, _, err = self.cpg("%s:/var/log/a.log" % self.c, dest)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read("renamed.log"), "a\n")


if __name__ == "__main__":
    unittest.main()
