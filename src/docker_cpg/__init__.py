"""docker cp with wildcards, several sources and excludes.

Copy out: the fixed directory in front of the first wildcard is streamed with
`docker cp CONTAINER:DIR/. -` and only matching tar members are written.  This
needs no shell or tar inside the container and works on stopped containers.

Copy in: the local sources are packed into one tar stream and sent with
`docker cp - CONTAINER:DIR`.
"""

import argparse
import fnmatch
import glob
import json
import os
import subprocess
import sys
import tarfile

__version__ = "0.1.0"

PROG = "docker-cpg"


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- matching


def has_magic(s):
    return any(c in s for c in "*?[")


def split_path(path):
    """'/var//log/./x' -> ['var', 'log', 'x']; '..' is rejected."""
    comps = [c for c in path.split("/") if c not in ("", ".")]
    if ".." in comps:
        raise UsageError("'..' is not supported in container paths: %s" % path)
    return comps


def match_component(name, pat):
    # Like the shell: '*' and '?' do not match a leading dot.
    if name.startswith(".") and not pat.startswith("."):
        return False
    return fnmatch.fnmatchcase(name, pat)


def match_prefix(pat, comps, min_len=0):
    """Length of the shortest prefix of `comps` (at least `min_len` long) that
    matches `pat` fully, else None.

    `pat` and `comps` are lists of path components.  '**' matches zero or more
    components (not hidden ones, like bash globstar).
    """
    best = None
    stack = [(0, 0)]
    seen = set()
    while stack:
        i, j = stack.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i == len(pat):
            if j >= min_len and (best is None or j < best):
                best = j
            continue
        if pat[i] == "**":
            stack.append((i + 1, j))
            if j < len(comps) and not comps[j].startswith("."):
                stack.append((i, j + 1))
            continue
        if j < len(comps) and match_component(comps[j], pat[i]):
            stack.append((i + 1, j + 1))
    return best


def compile_excludes(patterns):
    out = []
    for p in patterns:
        if "/" in p.strip("/"):
            out.append(("path", split_path(p)))
        else:
            out.append(("name", p.strip("/")))
    return out


def is_excluded(excludes, comps):
    """`comps` is the path relative to the directory the copy starts from."""
    for kind, pat in excludes:
        if kind == "name":
            if any(fnmatch.fnmatchcase(c, pat) for c in comps):
                return True
        elif match_prefix(pat, comps) is not None:
            return True
    return False


# ---------------------------------------------------------------- arguments


def parse_container_arg(arg):
    """Return (container, path) for 'CONTAINER:PATH', None for a local path.

    Same rule as docker: an argument starting with '/' or '.' is local,
    otherwise text before the first ':' names a container.
    """
    if arg.startswith(("/", ".")) or ":" not in arg:
        return None
    container, path = arg.split(":", 1)
    if not container:
        return None
    return container, path


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        description="docker cp with wildcards, several sources and excludes.",
        epilog=(
            "Copy out: %(prog)s [OPTIONS] CONTAINER:PATTERN... DEST|-\n"
            "Copy in:  %(prog)s [OPTIONS] SRC... CONTAINER:DIR\n\n"
            "Quote container patterns so the local shell does not expand them.\n"
            "A single source without wildcards is passed to docker cp unchanged."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="+", metavar="PATH")
    p.add_argument("-e", "--exclude", action="append", default=[], metavar="PATTERN",
                   help="skip matching files and directories; a pattern without '/' "
                        "matches any name, one with '/' matches the path relative to "
                        "the directory in front of the first wildcard (copy out) or "
                        "to the source's parent (copy in) (repeatable)")
    p.add_argument("-P", "--parents", action="store_true",
                   help="keep the full source path below DEST (like cp --parents)")
    p.add_argument("-a", "--archive", action="store_true",
                   help="keep uid/gid from the source (like docker cp -a)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="print what would be copied, write nothing")
    p.add_argument("-q", "--quiet", action="store_true", help="no summary on stderr")
    p.add_argument("--docker", default=os.environ.get("DOCKER_CPG_DOCKER", "docker"),
                   metavar="CMD", help="docker executable (default: docker)")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return p


# ---------------------------------------------------------------- copy out


class Copier:
    def __init__(self, args, dest):
        self.args = args
        self.dest = dest
        self.excludes = compile_excludes(args.exclude)
        self.roots = {}      # destination root path -> container path it came from
        self.renamed = {}    # archive name -> destination name (for hard links)
        self.skipped_roots = set()
        self.files = 0
        self.matched = []
        self.out_tar = None
        self.dirs = []
        self.failed = False
        if dest == "-" and not args.dry_run:
            self.out_tar = tarfile.open(fileobj=sys.stdout.buffer, mode="w|")
        elif not args.dry_run:
            os.makedirs(dest, exist_ok=True)
            self.dest_real = os.path.realpath(dest)

    def warn(self, msg):
        print("%s: %s" % (PROG, msg), file=sys.stderr)

    def copy_out(self, container, pattern):
        """Copy every match of `pattern` in `container`. Returns number of matches."""
        pat = split_path(pattern)
        base = []
        for c in pat:
            if has_magic(c):
                break
            base.append(c)
        rest = pat[len(base):]
        if not pat:
            raise UsageError("refusing to copy the whole container filesystem: %s" % pattern)
        if rest:
            src = "/" + "/".join(base + ["."])
        else:
            # A plain path among several sources: stream only that item.
            src = "/" + "/".join(base)
            base, rest = base[:-1], base[-1:]
        cmd = self.args.docker_cmd + ["cp", "%s:%s" % (container, src), "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        matches = 0
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                for member in tf:
                    comps = split_member(member.name)
                    if comps is None:
                        continue
                    k = match_prefix(rest, comps, min_len=1)
                    if k is None:
                        continue
                    root = base + comps[:k]
                    below = comps[k:]
                    rel = comps  # path below the fixed directory, for --exclude
                    if self.handle(tf, member, root, below, rel, container) and not below:
                        matches += 1
                        self.matched.append("/" + "/".join(root))
        except tarfile.ReadError:
            pass  # empty stream: docker already printed why
        finally:
            proc.stdout.close()
            rc = proc.wait()
            self.finish_dirs()
        if rc != 0 and matches:
            self.warn("docker cp exited with %d, copy of %s:%s may be incomplete"
                      % (rc, container, pattern))
            self.failed = True
        return matches

    def handle(self, tf, member, root, below, rel, container):
        """Write one tar member; returns False if it was excluded or skipped."""
        if is_excluded(self.excludes, rel):
            return False
        root_key = "/".join(root if self.args.parents else root[-1:])
        src_root = "%s:/%s" % (container, "/".join(root))
        if root_key in self.skipped_roots:
            return False
        if not below:
            prev = self.roots.get(root_key)
            if prev is not None and prev != src_root:
                self.warn("skipping %s: %s was already copied to the same name "
                          "(use --parents)" % (src_root, prev))
                self.skipped_roots.add(root_key)
                return False
            self.roots[root_key] = src_root
        new_name = root_key + "".join("/" + c for c in below)
        if self.args.dry_run:
            if not below:
                target = "-" if self.dest == "-" else os.path.join(self.dest, new_name)
                print("%s -> %s" % (src_root, target))
            return True
        old_name = "/".join(split_member(member.name))
        if member.islnk():
            target = self.renamed.get("/".join(split_member(member.linkname) or []))
            if target is None:
                self.warn("skipping hard link %s: its target was not copied" % new_name)
                return False
            member.linkname = target
        self.renamed[old_name] = new_name
        member.name = new_name
        if not self.args.archive and self.out_tar is None:
            member.uid, member.gid = os.getuid(), os.getgid()
            member.uname = member.gname = ""
        if member.isfile():
            self.files += 1
        if self.out_tar is not None:
            fileobj = tf.extractfile(member) if member.isfile() else None
            self.out_tar.addfile(member, fileobj)
            return True
        self.extract(tf, member)
        return True

    def extract(self, tf, member):
        path = os.path.join(self.dest, member.name)
        parent = os.path.realpath(os.path.dirname(path))
        if os.path.commonpath([parent, self.dest_real]) != self.dest_real:
            raise RuntimeError("refusing to write outside %s: %s" % (self.dest, member.name))
        if member.isdir():
            # Permissions are set at the end, so read-only directories still
            # receive their contents.
            if os.path.islink(path):
                os.unlink(path)
            os.makedirs(path, exist_ok=True)
            self.dirs.append((path, member.mode, member.mtime))
            return
        if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
            os.unlink(path)
        kwargs = {}
        if hasattr(tarfile, "fully_trusted_filter"):
            # Paths are checked above; keep modes and absolute symlinks like docker cp.
            kwargs["filter"] = "fully_trusted"
        tf.extract(member, self.dest, set_attrs=True, **kwargs)

    def finish_dirs(self):
        for path, mode, mtime in reversed(self.dirs):
            os.chmod(path, mode & 0o7777)
            os.utime(path, (mtime, mtime))
        self.dirs = []

    def close(self):
        if self.out_tar is not None:
            self.out_tar.close()
            sys.stdout.buffer.flush()


def split_member(name):
    comps = [c for c in name.split("/") if c not in ("", ".")]
    if ".." in comps:
        return None
    return comps


# ---------------------------------------------------------------- copy in


def expand_local(sources):
    out = []
    missing = []
    for s in sources:
        if os.path.lexists(s) or not has_magic(s):
            if os.path.lexists(s):
                out.append(s)
            else:
                missing.append(s)
            continue
        hits = sorted(glob.glob(s))
        if hits:
            out.extend(hits)
        else:
            missing.append(s)
    return out, missing


def copy_in(args, sources, container, dest_path):
    paths, missing = expand_local(sources)
    for m in missing:
        print("%s: no such file or no match: %s" % (PROG, m), file=sys.stderr)
    if not paths:
        return 1
    excludes = compile_excludes(args.exclude)
    used = {}

    def make_filter(arcname):
        skip = len(split_member(arcname)) - 1  # leading dirs added by --parents

        def tar_filter(info):
            if is_excluded(excludes, split_member(info.name)[skip:]):
                return None
            if not args.archive:
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
            return info
        return tar_filter

    entries = []
    for path in paths:
        norm = os.path.normpath(path)
        if args.parents:
            arcname = os.path.relpath(os.path.abspath(norm), "/") if os.path.isabs(norm) \
                else norm
        else:
            arcname = os.path.basename(os.path.abspath(norm))
        if arcname.startswith("..") or arcname in ("", "."):
            arcname = os.path.basename(os.path.abspath(norm))
        if arcname in used:
            print("%s: skipping %s: %s already uses the name %s (use --parents)"
                  % (PROG, path, used[arcname], arcname), file=sys.stderr)
            continue
        used[arcname] = path
        entries.append((path, arcname))

    target = "%s:%s" % (container, dest_path or "/")
    if args.dry_run:
        for path, arcname in entries:
            print("%s -> %s/%s" % (path, target.rstrip("/"), arcname))
        return 1 if missing else 0

    proc = subprocess.Popen(args.docker_cmd + ["cp", "-", target], stdin=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:
            for path, arcname in entries:
                tf.add(path, arcname=arcname, recursive=True, filter=make_filter(arcname))
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
    rc = proc.wait()
    if rc != 0:
        return rc
    if not args.quiet:
        print("%s: copied %d item(s) to %s" % (PROG, len(entries), target), file=sys.stderr)
    return 1 if missing else 0


# ---------------------------------------------------------------- main


def plugin_metadata():
    return json.dumps({
        "SchemaVersion": "0.1.0",
        "Vendor": "Jwrede",
        "Version": __version__,
        "ShortDescription": "docker cp with wildcards, several sources and excludes",
        "URL": "https://github.com/Jwrede/docker-cpg",
    })


def run(argv):
    if argv[:1] == ["docker-cli-plugin-metadata"]:
        print(plugin_metadata())
        return 0
    global_flags = []
    if "DOCKER_CLI_PLUGIN_ORIGINAL_CLI_COMMAND" in os.environ and "cpg" in argv:
        # Run as `docker [GLOBAL FLAGS] cpg ...`: keep flags such as --context.
        i = argv.index("cpg")
        global_flags, argv = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    args.docker_cmd = [args.docker] + global_flags
    if len(args.paths) < 2:
        raise UsageError("need at least one source and a destination")
    *sources, dest = args.paths
    dest_c = parse_container_arg(dest)
    src_c = [parse_container_arg(s) for s in sources]

    if dest_c is not None:
        if any(s is not None for s in src_c):
            raise UsageError("copying between containers is not supported")
        container, path = dest_c
        simple = (len(sources) == 1 and not has_magic(sources[0]) and not args.exclude
                  and not args.parents and not args.dry_run)
        if simple:
            return passthrough(args, sources[0], dest)
        return copy_in(args, sources, container, path)

    if any(s is None for s in src_c):
        raise UsageError("mixing local and container sources is not supported; "
                         "the destination decides the direction")
    simple = (len(sources) == 1 and not has_magic(src_c[0][1]) and not args.exclude
              and not args.parents and not args.dry_run)
    if simple:
        return passthrough(args, sources[0], dest)

    copier = Copier(args, dest)
    status = 0
    try:
        for container, pattern in src_c:
            if copier.copy_out(container, pattern) == 0:
                print("%s: no match: %s:%s" % (PROG, container, pattern), file=sys.stderr)
                status = 1
    finally:
        copier.close()
    if copier.failed:
        status = 1
    if not args.quiet and not args.dry_run:
        print("%s: copied %d match(es), %d file(s) to %s"
              % (PROG, len(copier.matched), copier.files, dest), file=sys.stderr)
    return status


def passthrough(args, src, dest):
    cmd = args.docker_cmd + ["cp"] + (["-a"] if args.archive else []) + [src, dest]
    return subprocess.call(cmd)


def main(argv=None):
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except UsageError as e:
        print("%s: %s" % (PROG, e), file=sys.stderr)
        return 2
    except (RuntimeError, OSError, tarfile.TarError) as e:
        print("%s: %s" % (PROG, e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
