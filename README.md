# docker-cpg

`docker cp` with wildcards, several sources and excludes. It also works on
stopped containers and on images without a shell or `tar`.

```console
$ docker cp "web:/var/log/app/*.log" logs/
Error response from daemon: Could not find the file /var/log/app/*.log in container web
$ docker cpg "web:/var/log/app/*.log" logs/
docker-cpg: copied 3 match(es), 3 file(s) to logs/
$ ls logs
access.log
debug.log
error.log
```

## Why

`docker cp` takes exactly one source and no wildcards:

- [moby/moby#7710](https://github.com/moby/moby/issues/7710) "support wildcards and
  destination names in `docker cp`", open since August 2014, 165 reactions. A maintainer
  wrote in 2017 that contributions are welcome but that correct wildcard matching is
  tricky. The thread offers only workarounds.
- Stack Overflow: [Copy folder with wildcard from docker container to host](https://stackoverflow.com/questions/35806102)
  (score 54, about 50k views) and [How to copy multiple files from container to host using docker cp](https://stackoverflow.com/questions/51265032)
  (score 20, about 15k views).
- Related open requests: several sources in one call
  ([moby#37961](https://github.com/moby/moby/issues/37961),
  [moby#50463](https://github.com/moby/moby/issues/50463)) and excludes when copying
  ([moby#40925](https://github.com/moby/moby/issues/40925), 22 reactions).

The usual workaround is `docker exec CONTAINER sh -c 'tar -cf - /path/*.log' | tar -xf -`.
It needs a running container that ships `sh` and `tar`, so it fails on stopped
containers, distroless and `FROM scratch` images.

## How it works

Copy out: `docker-cpg` takes the fixed directory in front of the first wildcard
(`/var/log/app` in `/var/log/app/*.log`), streams it with
`docker cp CONTAINER:/var/log/app/. -` and writes only the tar members that match.
Nothing runs inside the container.

Copy in: the local sources are packed into one tar stream and sent with
`docker cp - CONTAINER:DIR`.

A single source without wildcards, excludes or other options is handed to `docker cp`
unchanged.

## Install

Python 3.9 or newer, no dependencies.

```sh
pipx install git+https://github.com/Jwrede/docker-cpg
```

Optional, to call it as `docker cpg` (Docker CLI plugin):

```sh
mkdir -p ~/.docker/cli-plugins
ln -s "$(which docker-cpg)" ~/.docker/cli-plugins/docker-cpg
```

As a plugin it passes global flags such as `--context` on to the `docker` calls it makes.

## Usage

```
docker-cpg [OPTIONS] CONTAINER:PATTERN... DEST|-     copy out
docker-cpg [OPTIONS] SRC... CONTAINER:DIR            copy in

  -e, --exclude PATTERN  skip matching files and directories (repeatable)
  -P, --parents          keep the full source path below DEST (like cp --parents)
  -a, --archive          keep uid/gid from the source (like docker cp -a)
  -n, --dry-run          print what would be copied, write nothing
  -q, --quiet            no summary on stderr
      --docker CMD       docker executable (default: docker, or $DOCKER_CPG_DOCKER)
```

Quote container patterns so that your shell does not try to expand them.

Patterns follow the shell: `*`, `?` and `[...]` match within one path component,
`**` matches any number of directories, and wildcards do not match names that start
with a dot unless the pattern does. A matched directory is copied with all its
contents.

With a wildcard, `DEST` is always a directory and is created if missing. Each match
lands in `DEST` under its own name. If two matches have the same name
(`/srv/*/x.log`), the first one wins and the others are skipped with a warning; use
`--parents` to keep them apart. `-` as `DEST` writes one tar stream to stdout.

Excludes: a pattern without `/` matches any file or directory name. A pattern with `/`
is matched against the path relative to the directory in front of the first wildcard
(copy out) or relative to the parent of each source (copy in). Excluding a directory
excludes everything in it.

Exit status: 0 on success, 1 if a source matched nothing or `docker cp` failed
(the other sources are still copied), 2 on usage errors.

## Examples

Preview what a recursive pattern would copy, without the `old/` directory:

```console
$ docker cpg -n -e old "web:/var/log/**/*.log" logs/
web:/var/log/app/access.log -> logs/access.log
web:/var/log/app/debug.log -> logs/debug.log
web:/var/log/app/error.log -> logs/error.log
```

From a stopped container, as a tar stream:

```console
$ docker stop web
$ docker cpg "web:/var/log/app/*.log" - | tar tv
docker-cpg: copied 3 match(es), 3 file(s) to -
-rw-r--r-- 0/0               7 2026-10-30 05:09 access.log
-rw-r--r-- 0/0               6 2026-10-30 05:09 debug.log
-rw-r--r-- 0/0               6 2026-10-30 05:09 error.log
```

Several local sources into a container, skipping caches:

```console
$ docker cpg -e "*.pyc" -e __pycache__ src/*.py config/ web:/app
docker-cpg: copied 3 item(s) to web:/app
```

## Limits

- Copy out streams the whole fixed directory in front of the first wildcard and
  filters on the host. `"web:/var/log/*.log"` reads all of `/var/log`, including
  subdirectories; `"web:/**/*.conf"` reads the whole container filesystem. Put as
  much fixed path in front of the wildcard as you can.
- Copy in: `CONTAINER:DIR` must be an existing directory (a `docker cp -` rule).
- Symbolic links are copied as links, as with `docker cp` without `-L`. A hard link
  whose target was not copied is skipped with a warning.
- No copying between two containers, no brace expansion (`{a,b}`; an unquoted one is
  expanded by your shell into several sources, which works), no `.dockerignore` parsing.
- Tested with Docker 27 on Linux (locally and on GitHub's ubuntu-24.04 runners).
  `--docker podman` is not tested.

## Development

```sh
python -m unittest discover -s tests -v
```

The integration tests import a small root filesystem with `docker import` (no shell,
no `tar`, containers never started) and need a running Docker daemon; without one they
are skipped unless `DOCKER_CPG_REQUIRE_DOCKER=1` is set, as in CI.

## License

MIT
