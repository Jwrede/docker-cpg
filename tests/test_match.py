import json
import unittest

import docker_cpg as m


def mp(pattern, path, min_len=0):
    return m.match_prefix(m.split_path(pattern), m.split_path(path), min_len)


class MatchTest(unittest.TestCase):
    def test_star_matches_one_component(self):
        self.assertEqual(mp("*.log", "a.log"), 1)
        self.assertIsNone(mp("*.log", "a.txt"))
        self.assertEqual(mp("*", "dir/file"), 1)

    def test_star_skips_hidden_names(self):
        self.assertIsNone(mp("*.log", ".hidden.log"))
        self.assertEqual(mp(".*.log", ".hidden.log"), 1)
        self.assertIsNone(mp("?a", ".a"))

    def test_question_mark_and_class(self):
        self.assertEqual(mp("file-?.txt", "file-1.txt"), 1)
        self.assertEqual(mp("file-[0-9].txt", "file-7.txt"), 1)
        self.assertIsNone(mp("file-[0-9].txt", "file-x.txt"))

    def test_double_star(self):
        self.assertEqual(mp("**/*.log", "a/b/c.log"), 3)
        self.assertEqual(mp("**/*.log", "c.log"), 1)
        self.assertIsNone(mp("**/*.log", ".git/c.log"))
        self.assertEqual(mp("**", "a/b"), 0)
        self.assertEqual(mp("**", "a/b", min_len=1), 1)

    def test_shortest_prefix_wins(self):
        # 'logs' matches as a directory before anything inside it.
        self.assertEqual(mp("**/logs*", "srv/logs/logs-old"), 2)

    def test_middle_wildcard(self):
        self.assertEqual(mp("*/x.log", "a/x.log/inner"), 2)
        self.assertIsNone(mp("*/x.log", "a/y.log"))

    def test_split_path_rejects_dotdot(self):
        with self.assertRaises(m.UsageError):
            m.split_path("/var/../etc")
        self.assertEqual(m.split_path("//var/./log/"), ["var", "log"])


class ExcludeTest(unittest.TestCase):
    def test_name_pattern_matches_any_component(self):
        ex = m.compile_excludes(["node_modules", "*.tmp"])
        self.assertTrue(m.is_excluded(ex, ["proj", "node_modules", "x", "i.js"]))
        self.assertTrue(m.is_excluded(ex, ["proj", "a.tmp"]))
        self.assertFalse(m.is_excluded(ex, ["proj", "lib", "m.py"]))

    def test_path_pattern_is_anchored_at_copy_root(self):
        ex = m.compile_excludes(["proj/lib"])
        self.assertTrue(m.is_excluded(ex, ["proj", "lib", "m.py"]))
        self.assertFalse(m.is_excluded(ex, ["other", "proj", "lib"]))


class ArgsTest(unittest.TestCase):
    def test_container_arg(self):
        self.assertEqual(m.parse_container_arg("web:/var/log/*.log"), ("web", "/var/log/*.log"))
        self.assertIsNone(m.parse_container_arg("./web:x"))
        self.assertIsNone(m.parse_container_arg("/tmp/a:b"))
        self.assertIsNone(m.parse_container_arg("plain"))

    def test_plugin_metadata(self):
        meta = json.loads(m.plugin_metadata())
        self.assertEqual(meta["SchemaVersion"], "0.1.0")
        self.assertIn("wildcards", meta["ShortDescription"])

    def test_usage_errors(self):
        self.assertEqual(m.main(["only-one"]), 2)
        self.assertEqual(m.main(["a:/x", "b:/y"]), 2)
        self.assertEqual(m.main(["a:/x", "local", "dest"]), 2)


if __name__ == "__main__":
    unittest.main()
