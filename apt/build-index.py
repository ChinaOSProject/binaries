#!/usr/bin/env python3
"""Build a signed APT snapshot from the .deb pointers in a Git commit."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen


SUITE = "stable"
KEYRING = "chinaos-archive-keyring.gpg"
MEDIA = "https://media.githubusercontent.com/media/ChinaOSProject/binaries"
DEB_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_~-]*/[a-z0-9-]+/"
                      r"[A-Za-z0-9][A-Za-z0-9.+_~-]*\.deb")
POINTER = re.compile(rb"version https://git-lfs.github.com/spec/v1\n"
                     rb"oid sha256:([a-f0-9]{64})\nsize ([1-9][0-9]*)\n")
INDEX_FIELDS = {"filename", "size", "md5sum", "sha1", "sha256", "sha512"}
CACHE_VERSION = "v1"


def command(*args):
    return subprocess.check_output(args)


def pointers(commit):
    for entry in command("git", "ls-tree", "-rlz", commit).split(b"\0"):
        if not entry:
            continue
        header, path = entry.decode("utf-8").split("\t", 1)
        if not path.endswith(".deb"):
            continue
        mode, kind, blob, size = header.split()
        if (not DEB_PATH.fullmatch(path) or mode not in {"100644", "100755"}
                or kind != "blob" or int(size) > 1024):
            raise ValueError(f"{path}: expected a regular Git LFS .deb pointer")
        match = POINTER.fullmatch(command("git", "cat-file", "blob", blob))
        if not match:
            raise ValueError(f"{path}: invalid Git LFS pointer")
        yield path, match[1].decode("ascii"), int(match[2])


def package_control(deb, oid, size):
    digest = hashlib.sha256()
    with deb.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if deb.stat().st_size != size or digest.hexdigest() != oid:
        raise ValueError(f"{deb}: size or SHA256 differs from LFS pointer ({oid})")
    return command("dpkg-deb", "--field", str(deb)).decode("utf-8")


def control_member_end(prefix):
    """Read the end offset of the .deb control archive from its ar header."""
    if not prefix.startswith(b"!<arch>\n"):
        return None
    offset = 8
    for expected in (rb"debian-binary", rb"control\.tar(?:\.[a-z0-9]+)?"):
        header = prefix[offset:offset + 60]
        if len(header) != 60 or header[58:60] != b"`\n":
            return None
        if not re.fullmatch(expected, header[:16].strip().rstrip(b"/")):
            return None
        length = header[48:58].strip()
        if not length.isdigit():
            return None
        size = int(length)
        offset += 60 + size + (size % 2)
    return offset


def media_range(url, oid, size, limit):
    request = Request(f"{url}?apt-control={limit}",
                      headers={"Range": f"bytes=0-{limit - 1}", "Accept-Encoding": "identity"})
    try:
        with urlopen(request, timeout=15) as response:
            if (response.status != 206 or response.headers.get("ETag") != f'"{oid}"'
                    or response.headers.get("Content-Range") != f"bytes 0-{limit - 1}/{size}"):
                return None
            prefix = response.read(limit + 1)
    except (OSError, IncompleteRead):
        return None
    return prefix if len(prefix) == limit else None


def read_media_prefix(url, oid, size, deb):
    limit = min(size, 65536)
    prefix = media_range(url, oid, size, limit)
    if prefix is None:
        return None
    end = control_member_end(prefix)
    if end is None or end > min(size, 16 * 1024 * 1024):
        return None
    if end > limit:
        prefix = media_range(url, oid, size, end)
    if prefix is None:
        return None
    # dpkg-deb --field needs only the ar members through control.tar.
    deb.write_bytes(prefix[:end])
    return command("dpkg-deb", "--field", str(deb)).decode("utf-8")


def read_control(path, oid, size, commit, root, lfs_objects, cache):
    cached = cache / f"{CACHE_VERSION}-{oid}-{size}.control"
    if cached.is_file():
        return cached.read_text(encoding="utf-8"), True
    local = lfs_objects / oid[:2] / oid[2:4] / oid
    if not local.is_file():
        local = root / path
    if local.is_file() and local.stat().st_size == size:
        return package_control(local, oid, size), False

    with tempfile.TemporaryDirectory(prefix="apt-lfs-") as directory:
        deb = Path(directory) / "package.deb"
        url = f"{MEDIA}/{commit}/{quote(path, safe='/')}"
        control = read_media_prefix(url, oid, size, deb)
        if control is not None:
            return control, False
        subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                        "--proto", "=https", "--proto-redir", "=https", "--retry", "3",
                        "--connect-timeout", "15", "--max-time", "900", "--max-filesize",
                        str(size), "--output", str(deb), url], check=True)
        return package_control(deb, oid, size), False


def parse_control(control):
    fields = {}
    for line in control.rstrip("\n").split("\n"):
        if line.startswith((" ", "\t")) and fields:
            fields[key][1].append(line)
            continue
        name, separator, value = line.partition(":")
        key = name.lower()
        if not separator or not re.fullmatch(r"[A-Za-z0-9-]+", name) or key in fields:
            raise ValueError(f"invalid Debian control field {name!r}")
        fields[key] = (name, [value])

    identity = []
    for key in ("package", "version", "architecture"):
        if key not in fields or len(fields[key][1]) != 1 or not fields[key][1][0].strip():
            raise ValueError(f"missing or invalid {key} field")
        identity.append(fields[key][1][0].strip())
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identity[2]):
        raise ValueError(f"invalid Debian architecture {identity[2]!r}")
    if identity[2] == "loong64":
        raise ValueError("package declares loong64; this repository uses loongarch64")
    preserved = [name + ":" + "\n".join(lines) for key, (name, lines) in fields.items()
                 if key not in INDEX_FIELDS]
    return tuple(identity), "\n".join(preserved)


def collect(commit, root, lfs_objects, cache):
    packages = {}
    entries = list(pointers(commit))
    for number, (path, oid, size) in enumerate(entries, 1):
        try:
            control, cached = read_control(path, oid, size, commit, root, lfs_objects, cache)
            identity, fields = parse_control(control)
            if not cached:
                (cache / f"{CACHE_VERSION}-{oid}-{size}.control").write_text(
                    control, encoding="utf-8")
            if identity in packages:
                previous_oid, _, previous_path = packages[identity]
                if previous_oid != oid:
                    raise ValueError(f"conflicting {' '.join(identity)} also at {previous_path}")
                continue
            filename = f"pool/{commit}/{path}"
            stanza = f"{fields}\nFilename: {filename}\nSize: {size}\nSHA256: {oid}\n\n"
            packages[identity] = (oid, stanza, path)
        except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as error:
            raise SystemExit(f"{path}: {error}") from error
        if number % 25 == 0 or number == len(entries):
            print(f"Read controls for {number}/{len(entries)} committed .deb files", flush=True)
    if not packages:
        raise SystemExit("No .deb packages found in the selected commit")
    return packages


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_snapshot(output, packages, fingerprint):
    # Derive immutable names before exposing the snapshot through the current marker.
    files = {}
    by_arch = defaultdict(list)
    for identity, (_, stanza, _) in sorted(packages.items()):
        by_arch[identity[2]].append(stanza)
    architectures = sorted(by_arch.keys() - {"all"})
    if not architectures:
        raise SystemExit("The repository needs at least one native binary architecture")
    for arch in architectures:
        name = f"main/binary-{arch}/Packages"
        data = "".join(sorted(by_arch[arch] + by_arch["all"])).encode("utf-8")
        files[name] = data
        files[f"{name}.gz"] = gzip.compress(data, mtime=0)

    now = datetime.now(timezone.utc)
    release = (
        "Origin: ChinaOSProject\nLabel: ChinaOSProject\n"
        f"Suite: {SUITE}\nCodename: {SUITE}\n"
        f"Date: {format_datetime(now, usegmt=True)}\n"
        f"Valid-Until: {format_datetime(now + timedelta(days=14), usegmt=True)}\n"
        f"Architectures: {' '.join(architectures)}\nComponents: main\n"
        "Description: ChinaOSProject binary packages\nAcquire-By-Hash: yes\n"
        "NotAutomatic: yes\nButAutomaticUpgrades: yes\nSHA256:\n"
    )
    for name, data in sorted(files.items()):
        digest = hashlib.sha256(data).hexdigest()
        release += f" {digest} {len(data):16d} {name}\n"
        by_hash = Path(name).parent / "by-hash" / "SHA256" / digest
        write(output / "objects" / "by-hash" / f"dists/{SUITE}" / by_hash, data)

    with tempfile.TemporaryDirectory(prefix="apt-release-") as directory:
        directory = Path(directory)
        (directory / "Release").write_text(release, encoding="utf-8")
        gpg = ("gpg", "--batch", "--no-tty", "--local-user", fingerprint,
               "--digest-algo", "SHA256")
        subprocess.run([*gpg, "--armor", "--output", str(directory / "InRelease"),
                        "--clearsign", str(directory / "Release")], check=True)
        subprocess.run([*gpg, "--armor", "--output", str(directory / "Release.gpg"),
                        "--detach-sign", str(directory / "Release")], check=True)
        in_release = (directory / "InRelease").read_bytes()
        snapshot = hashlib.sha256(in_release).hexdigest()
        root = output / "objects" / "snapshots" / snapshot
        dists = root / "dists" / SUITE
        for name in ("InRelease", "Release", "Release.gpg"):
            write(dists / name, (directory / name).read_bytes())
        for name, data in files.items():
            write(dists / name, data)
        keyring = command("gpg", "--batch", "--export-options", "export-minimal",
                          "--export", fingerprint)
        if not keyring:
            raise SystemExit("Failed to export the APT signing key")
        write(root / KEYRING, keyring)
        write(output / "current", f"{snapshot}\n".encode("ascii"))
    print(json.dumps({"snapshot": snapshot, "packages": len(packages),
                      "architectures": architectures}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--cache", type=Path, required=True, help="Cache of verified package controls")
    parser.add_argument("--fingerprint", default=os.environ.get("APT_SIGNING_FINGERPRINT"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Fa-f0-9]{40}", args.fingerprint or ""):
        parser.error("provide the full 40-hex-digit APT signing key fingerprint")
    if args.output.exists():
        parser.error("--output must be a new directory")

    commit = command("git", "rev-parse", "--verify", f"{args.commit}^{{commit}}").decode().strip()
    root = Path(command("git", "rev-parse", "--show-toplevel").decode().strip())
    common = Path(command("git", "rev-parse", "--git-common-dir").decode().strip())
    args.cache.mkdir(parents=True, exist_ok=True)
    packages = collect(commit, root, common / "lfs" / "objects", args.cache)
    build_snapshot(args.output, packages, args.fingerprint.upper())


if __name__ == "__main__":
    main()
