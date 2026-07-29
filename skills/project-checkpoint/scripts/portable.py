#!/usr/bin/env python3
"""Create and verify a portable project-checkpoint source bundle."""

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import sys
import tempfile
import time
import zipfile

import checkpoint

SCHEMA = 2
MAX_ENTRIES = 20_000
RESERVED = {".project-checkpoint/START_HERE.md", ".project-checkpoint/bundle.json"}
CATEGORIES = ("design-ui", "assets", "source", "config", "tests", "docs", "data", "vendor", "release", "other")
ARCHIVE_SUFFIXES = {".7z", ".apk", ".bz2", ".dmg", ".gz", ".ipa", ".rar", ".tar", ".tgz", ".xz", ".zip"}
DESIGN_UI_SUFFIXES = {
    ".astro", ".css", ".htm", ".html", ".jsx", ".less", ".sass", ".scss", ".styl",
    ".svelte", ".svg", ".tsx", ".vue",
}
ASSET_SUFFIXES = {".avif", ".gif", ".ico", ".jpeg", ".jpg", ".otf", ".png", ".ttf", ".webp", ".woff", ".woff2"}
SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".kt", ".m",
    ".mm", ".php", ".py", ".rb", ".rs", ".sh", ".swift", ".ts",
}
DATA_SUFFIXES = {".csv", ".json", ".jsonl", ".ndjson", ".sql", ".tsv", ".xml", ".yaml", ".yml"}
DOC_SUFFIXES = {".adoc", ".md", ".mdx", ".rst", ".txt"}
UI_DIRS = {"components", "design", "layouts", "pages", "styles", "ui", "views"}
CONFIG_NAMES = {
    "cargo.toml", "composer.json", "deno.json", "deno.jsonc", "gemfile", "go.mod",
    "package-lock.json", "package.json", "pnpm-lock.yaml", "pyproject.toml",
    "requirements.txt", "yarn.lock",
}
STORED_SUFFIXES = {
    ".7z", ".apk", ".avif", ".bz2", ".docx", ".gif", ".gz", ".heic", ".ico",
    ".jpeg", ".jpg", ".mp3", ".mp4", ".pdf", ".png", ".pptx", ".rar", ".webm",
    ".webp", ".xlsx", ".xz", ".zip",
}
SECRET_FILENAMES = {
    ".env", ".netrc", "credentials", "credentials.json", "id_dsa", "id_ed25519",
    "id_ecdsa", "id_rsa", "service-account.json",
}
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
SECRET_BYTES = [
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\b(?:ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(rb"(?i)https?://[^\s/@:]+:[^\s/@]+@"),
    re.compile(rb"(?i)(?:^|\n)\s*//[^\n:]+:_authToken\s*=\s*[^\s$][^\s]*"),
]


def fail(message):
    raise checkpoint.CheckpointError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def safe_relative(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise checkpoint.CheckpointError("Portable bundles require UTF-8 file names.") from e
    path = PurePosixPath(text)
    if not text or len(text.encode("utf-8")) > 65_000 or path.is_absolute() or "\\" in text or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"Unsafe bundle path: {checkpoint.display_path(raw)}")
    for part in path.parts:
        stem = part.split(".", 1)[0].upper()
        if ":" in part or part.endswith((" ", ".")) or any(ord(char) < 32 for char in part) or stem in WINDOWS_DEVICES:
            fail(f"Non-portable bundle path: {checkpoint.display_path(raw)}")
    if path.parts[0] == ".git" or path.as_posix() in RESERVED:
        fail(f"Reserved bundle path: {path.as_posix()}")
    return path.as_posix()


def archive_prefix(root):
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip(".-")
    return name or "project"


def visible_paths(root, meta):
    raw = checkpoint.run_git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
    ).stdout
    paths = {path for path in checkpoint.split_z(raw) if path}
    paths.difference_update(path.encode("utf-8") for path in RESERVED)
    required = {"HANDOFF.md", *meta["references"]}
    if meta.get("plan"):
        required.add(meta["plan"])
    paths.update(path.encode("utf-8") for path in required)
    if len(paths) > MAX_ENTRIES:
        fail(f"Bundle exceeds {MAX_ENTRIES} files; reduce generated or untracked project content.")
    return sorted(paths), required


def classify_path(rel):
    path = PurePosixPath(rel)
    parts = {part.lower() for part in path.parts[:-1]}
    name = path.name.lower()
    suffix = path.suffix.lower()
    if "release" in parts or "releases" in parts or suffix in ARCHIVE_SUFFIXES:
        return "release"
    if "vendor" in parts or "third_party" in parts or "third-party" in parts:
        return "vendor"
    if (
        "test" in parts
        or "tests" in parts
        or "spec" in parts
        or "specs" in parts
        or (suffix in SOURCE_SUFFIXES | {".cjs", ".mjs"} and re.search(r"(?:^test[._-]|[._-](?:test|spec)\.[^.]+$)", name))
    ):
        return "tests"
    if suffix in ASSET_SUFFIXES or parts & {"assets", "fonts", "icons", "images"}:
        return "assets"
    if (
        suffix in DESIGN_UI_SUFFIXES
        or (parts & UI_DIRS and suffix in SOURCE_SUFFIXES | DATA_SUFFIXES | DOC_SUFFIXES)
        or (path.parts and path.parts[0].lower() in {"app", "public"} and suffix in SOURCE_SUFFIXES | DATA_SUFFIXES | DOC_SUFFIXES)
    ):
        return "design-ui"
    if "docs" in parts or name.startswith(("readme", "license", "contributing", "changelog")) or suffix in DOC_SUFFIXES:
        return "docs"
    if (
        name in CONFIG_NAMES
        or name.endswith((".config.js", ".config.mjs", ".config.cjs", ".config.ts", ".lock"))
        or name.startswith((".", "dockerfile"))
    ):
        return "config"
    if suffix in SOURCE_SUFFIXES:
        return "source"
    if suffix in DATA_SUFFIXES:
        return "data"
    return "other"


def source_paths(root, meta, include_categories):
    paths, required = visible_paths(root, meta)
    selected = []
    omitted = set()
    include = set(CATEGORIES) if "all" in include_categories else set(include_categories) | {"design-ui"}
    for raw in paths:
        rel = safe_relative(raw)
        category = classify_path(rel)
        if rel in required or category in include:
            selected.append(raw)
        else:
            omitted.add(category)
    return selected, required, sorted(omitted)


def path_size(root, raw):
    native = os.path.join(os.fsencode(root), raw)
    try:
        info = os.lstat(native)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return len(os.fsencode(os.readlink(native)))
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def analyze(project):
    root = checkpoint.git_root(project)
    if checkpoint.resume(root)["resume_status"] != "fresh":
        fail("HANDOFF.md is not fresh for the current branch and working tree; publish a new checkpoint first.")
    handoff_data, _ = checkpoint.read_regular(root / "HANDOFF.md")
    meta = checkpoint.parse_handoff(handoff_data.decode("utf-8"))
    raw_paths, required = visible_paths(root, meta)
    groups = {category: {"file_count": 0, "source_bytes": 0, "sample": []} for category in CATEGORIES}
    required_summary = {"file_count": 0, "source_bytes": 0, "sample": []}
    for raw in raw_paths:
        rel = safe_relative(raw)
        size = path_size(root, raw)
        if size is None:
            continue
        summary = required_summary if rel in required else groups[classify_path(rel)]
        summary["file_count"] += 1
        summary["source_bytes"] += size
        if len(summary["sample"]) < 8:
            summary["sample"].append(rel)
    return {
        "project": root.name,
        "auto_categories": ["design-ui"],
        "required": required_summary,
        "categories": groups,
    }


def reject_secret_path(path):
    base = PurePosixPath(path).name.lower()
    allowed_env = base.endswith((".example", ".sample", ".template"))
    if base in SECRET_FILENAMES or (base.startswith(".env.") and not allowed_env):
        fail(f"Likely secret file refused: {path}")


def scan_secret(chunk, tail, path):
    sample = tail + chunk
    for pattern in SECRET_BYTES:
        if pattern.search(sample):
            fail(f"Potential credential detected in {path}; remove or redact it before bundling.")
    return sample[-512:]


def zip_info(name, mode, mtime, kind="file"):
    stamp = time.localtime(mtime)[:6]
    if stamp[0] < 1980 or stamp[0] > 2107:
        stamp = (1980, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(name, stamp)
    info.create_system = 3
    bits = stat.S_IFLNK | 0o777 if kind == "symlink" else stat.S_IFREG | mode
    info.external_attr = bits << 16
    info.compress_type = zipfile.ZIP_STORED if kind == "symlink" or Path(name).suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED
    return info


def validate_symlink_target(rel, target_raw):
    try:
        target_text = target_raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise checkpoint.CheckpointError(f"Portable bundles require UTF-8 symlink targets: {rel}") from e
    if posixpath.isabs(target_text) or "\\" in target_text:
        fail(f"Non-portable symlink refused: {rel}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target_text))
    if resolved == ".." or resolved.startswith("../"):
        fail(f"Escaping symlink refused: {rel}")
    return target_text


def add_path(zf, root, raw, rel, prefix, total):
    native = os.path.join(os.fsencode(root), raw)
    try:
        before = os.lstat(native)
    except FileNotFoundError:
        return None, total
    arcname = f"{prefix}/{rel}"
    reject_secret_path(rel)
    if stat.S_ISLNK(before.st_mode):
        target = os.readlink(native)
        target_raw = os.fsencode(target)
        validate_symlink_target(rel, target_raw)
        digest = hashlib.sha256(target_raw).hexdigest()
        zf.writestr(zip_info(arcname, 0o777, before.st_mtime, "symlink"), target_raw)
        return {"path": rel, "sha256": digest, "size": len(target_raw), "mode": "120000", "type": "symlink"}, total
    if stat.S_ISDIR(before.st_mode):
        fail(f"Directory or submodule path cannot be bundled as one file: {rel}; checkpoint it separately.")
    if not stat.S_ISREG(before.st_mode):
        fail(f"Special file refused: {rel}")
    if before.st_size > checkpoint.MAX_FILE:
        fail(f"File exceeds 512 MiB bundle limit: {rel}")
    if total + before.st_size > checkpoint.MAX_TOTAL:
        fail("Bundle source exceeds the 2 GiB aggregate limit.")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(native, flags)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EAGAIN, errno.ENXIO):
            fail(f"Unsafe or blocking path refused: {rel}")
        raise
    digest = hashlib.sha256()
    size = 0
    tail = b""
    identity = lambda value: (value.st_mode, value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, getattr(value, "st_ctime_ns", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            fail(f"Path changed during safe open: {rel}")
        mode = stat.S_IMODE(opened.st_mode)
        with zf.open(zip_info(arcname, mode, opened.st_mtime), "w", force_zip64=True) as dest:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > checkpoint.MAX_FILE:
                    fail(f"File exceeds 512 MiB bundle limit: {rel}")
                digest.update(chunk)
                tail = scan_secret(chunk, tail, rel)
                dest.write(chunk)
        if identity(opened) != identity(os.fstat(fd)):
            fail(f"File changed while bundling: {rel}")
    finally:
        os.close(fd)
    return {
        "path": rel,
        "sha256": digest.hexdigest(),
        "size": size,
        "mode": f"{stat.S_IMODE(opened.st_mode):06o}",
        "type": "file",
    }, total + size


def output_authorization(output, approved):
    try:
        current = os.lstat(output)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(current.st_mode):
        fail(f"Existing bundle output must be a regular file: {output}")
    if not approved:
        fail(f"Refusing to overwrite existing bundle: {output}; explicitly approve that exact path.")
    return (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)


def output_unchanged(output, identity):
    try:
        current = os.lstat(output)
    except FileNotFoundError:
        return identity is None
    return identity is not None and stat.S_ISREG(current.st_mode) and (
        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
    ) == identity


def start_here(project, schema=SCHEMA):
    if schema == 1:
        return (
            "# Portable Project Checkpoint\n\n"
            "Open `HANDOFF.md` first, then inspect the included source files before changing anything.\n\n"
            "This archive contains the Git-visible working-tree snapshot, including tracked files and "
            "non-ignored untracked files, plus explicitly referenced regular files. `.git`, Git history, "
            "deleted paths, and unreferenced ignored files are intentionally absent. The branch and commit "
            "in `HANDOFF.md` identify the saved "
            "state but cannot reconstruct history from this archive alone. Do not claim saved verification "
            "is current after editing the source.\n\n"
            f"Project directory: `{project}`\n"
        ).encode()
    return (
        "# Portable Project Checkpoint\n\n"
        "Open `HANDOFF.md` first, then inspect the included source files before changing anything.\n\n"
        "This archive contains the selected design/UI working set, explicitly requested categories, "
        "and required checkpoint references. Other Git-visible files may be intentionally absent. "
        "`.git`, Git history, deleted paths, and Git-ignored files are absent. The branch and commit in "
        "`HANDOFF.md` identify the saved state but cannot reconstruct history from this archive alone. "
        "Do not claim saved verification is current after editing the source.\n\n"
        f"Project directory: `{project}`\n"
    ).encode()


def create(project, output, approved=False, include_categories=None):
    root = checkpoint.git_root(project)
    output = Path(output).expanduser()
    if not output.is_absolute():
        fail("--output must be an absolute path.")
    output = output.parent.resolve() / output.name
    if output == root or root in output.parents:
        fail("Portable bundle output must be outside the selected worktree.")
    if output.suffix.lower() != ".zip":
        fail("Portable bundle output must end in .zip.")
    if not output.parent.is_dir():
        fail(f"Bundle output directory does not exist: {output.parent}")
    output_state = output_authorization(output, approved)
    resume = checkpoint.resume(root)
    if resume["resume_status"] != "fresh":
        fail("HANDOFF.md is not fresh for the current branch and working tree; publish a new checkpoint first.")
    handoff_data, _ = checkpoint.read_regular(root / "HANDOFF.md")
    meta = checkpoint.parse_handoff(handoff_data.decode("utf-8"))
    include_categories = include_categories or []
    raw_paths, required, omitted_categories = source_paths(root, meta, include_categories)
    prefix = archive_prefix(root)
    files = []
    total = 0
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as zf:
            for raw in raw_paths:
                rel = safe_relative(raw)
                item, total = add_path(zf, root, raw, rel, prefix, total)
                if item is None:
                    if rel in required:
                        fail(f"Required checkpoint file is missing: {rel}")
                    continue
                files.append(item)
            if "HANDOFF.md" not in {item["path"] for item in files}:
                fail("Portable bundle requires HANDOFF.md.")
            guide = start_here(prefix, SCHEMA)
            zf.writestr(zip_info(f"{prefix}/.project-checkpoint/START_HERE.md", 0o644, time.time()), guide)
            manifest = {
                "schema": SCHEMA,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "project": prefix,
                "branch": meta["branch"],
                "commit": meta["commit"],
                "fingerprint": meta["fingerprint"],
                "handoff": "HANDOFF.md",
                "files": files,
                "omitted": [
                    ".git and Git history",
                    "deleted paths",
                    "Git-ignored files",
                    *([f"Git-visible categories not selected: {', '.join(omitted_categories)}"] if omitted_categories else []),
                ],
            }
            zf.writestr(zip_info(f"{prefix}/.project-checkpoint/bundle.json", 0o644, time.time()), canonical(manifest))
        verify(temporary)
        if checkpoint.resume(root)["resume_status"] != "fresh":
            fail("Repository changed while bundling; publish a new checkpoint and retry.")
        if not output_unchanged(output, output_state):
            fail("Bundle output changed after overwrite authorization; retry.")
        os.replace(temporary, output)
        checkpoint.fsync_directory(output.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    result = verify(output)
    result.update({
        "created": True,
        "included_categories": sorted(set(CATEGORIES) if "all" in include_categories else set(include_categories) | {"design-ui"}),
        "output": os.fspath(output),
    })
    return result


def validate_zip_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or "\\" in name or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"Unsafe archive member: {name}")
    return path


def verify(bundle):
    bundle = Path(bundle).expanduser()
    bundle = bundle.parent.resolve() / bundle.name
    if not bundle.is_file() or bundle.is_symlink():
        fail(f"Bundle must be a regular ZIP file: {bundle}")
    archive_digest = hashlib.sha256()
    with bundle.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            archive_digest.update(chunk)
    with zipfile.ZipFile(bundle) as zf:
        if zf.comment:
            fail("Bundle ZIP comments are not allowed.")
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES + 2:
            fail("Bundle contains too many entries.")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            fail("Bundle contains duplicate entries.")
        for name in names:
            validate_zip_path(name)
        manifests = [name for name in names if name.endswith("/.project-checkpoint/bundle.json")]
        if len(manifests) != 1:
            fail("Bundle must contain exactly one canonical manifest.")
        manifest_raw = zf.read(manifests[0])
        try:
            manifest = json.loads(manifest_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise checkpoint.CheckpointError("Bundle manifest is invalid JSON.") from e
        if canonical(manifest) != manifest_raw:
            fail("Bundle manifest is not canonical.")
        required_keys = {"schema", "created_at", "project", "branch", "commit", "fingerprint", "handoff", "files", "omitted"}
        if not isinstance(manifest, dict) or set(manifest) != required_keys or manifest["schema"] not in {1, SCHEMA}:
            fail("Bundle manifest schema is invalid.")
        if (
            not all(isinstance(manifest[key], str) for key in ("created_at", "project", "branch", "commit", "fingerprint", "handoff"))
            or not isinstance(manifest["files"], list)
            or not isinstance(manifest["omitted"], list)
            or not all(isinstance(value, str) for value in manifest["omitted"])
        ):
            fail("Bundle manifest field types are invalid.")
        prefix = manifest["project"]
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?", prefix):
            fail("Bundle project prefix is invalid.")
        if (
            not re.fullmatch(r"(?:unborn|[0-9a-f]{40,64})", manifest["commit"])
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["fingerprint"])
        ):
            fail("Bundle Git identity is invalid.")
        try:
            time.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            fail("Bundle creation time is invalid.")
        expected = {
            f"{prefix}/.project-checkpoint/START_HERE.md",
            f"{prefix}/.project-checkpoint/bundle.json",
        }
        if zf.read(f"{prefix}/.project-checkpoint/START_HERE.md") != start_here(prefix, manifest["schema"]):
            fail("Bundle resume instructions failed integrity verification.")
        info_by_name = {info.filename: info for info in infos}
        total = 0
        listed = set()
        for item in manifest["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "size", "mode", "type"}:
                fail("Bundle file manifest entry is invalid.")
            rel = item["path"]
            safe_relative(rel.encode("utf-8"))
            if rel in listed or item["type"] not in {"file", "symlink"}:
                fail("Bundle file manifest contains a duplicate or invalid type.")
            if not isinstance(item["size"], int) or item["size"] < 0 or item["size"] > checkpoint.MAX_FILE:
                fail("Bundle file size is invalid.")
            if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) or not re.fullmatch(r"(?:120000|0[0-7]{5})", item["mode"]):
                fail("Bundle file digest or mode is invalid.")
            name = f"{prefix}/{rel}"
            if name not in names:
                fail(f"Bundle member is missing: {rel}")
            info = info_by_name[name]
            archive_mode = info.external_attr >> 16
            expected_kind = stat.S_IFLNK if item["type"] == "symlink" else stat.S_IFREG
            expected_mode = 0o777 if item["type"] == "symlink" else int(item["mode"], 8)
            if stat.S_IFMT(archive_mode) != expected_kind or stat.S_IMODE(archive_mode) != expected_mode:
                fail(f"Bundle member mode failed integrity verification: {rel}")
            if info.file_size != item["size"]:
                fail(f"Bundle member size failed integrity verification: {rel}")
            digest = hashlib.sha256()
            size = 0
            symlink_data = bytearray()
            with zf.open(info) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > checkpoint.MAX_FILE:
                        fail(f"Bundle member exceeds 512 MiB: {rel}")
                    digest.update(chunk)
                    if item["type"] == "symlink":
                        symlink_data.extend(chunk)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                fail(f"Bundle member failed integrity verification: {rel}")
            if item["type"] == "symlink":
                if size > 4096:
                    fail(f"Bundle symlink target is too large: {rel}")
                validate_symlink_target(rel, bytes(symlink_data))
            total += size
            if total > checkpoint.MAX_TOTAL:
                fail("Bundle exceeds the 2 GiB aggregate limit.")
            listed.add(rel)
            expected.add(name)
        if set(names) != expected:
            fail("Bundle contains unmanifested or missing members.")
        handoff_name = f"{prefix}/{manifest['handoff']}"
        if manifest["handoff"] != "HANDOFF.md" or handoff_name not in names:
            fail("Bundle handoff path is invalid.")
        try:
            checkpoint.parse_handoff(zf.read(handoff_name).decode("utf-8"))
        except UnicodeDecodeError as e:
            raise checkpoint.CheckpointError("Bundled HANDOFF.md is not UTF-8.") from e
    return {
        "verified": True,
        "bundle": os.fspath(bundle),
        "project": prefix,
        "branch": manifest["branch"],
        "commit": manifest["commit"],
        "file_count": len(manifest["files"]),
        "source_bytes": total,
        "sha256": archive_digest.hexdigest(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    analyze_parser = commands.add_parser("analyze")
    analyze_parser.add_argument("--project", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--project", required=True)
    create_parser.add_argument("--output", required=True)
    create_parser.add_argument("--approve-overwrite", action="store_true")
    create_parser.add_argument("--include-category", action="append", choices=[*CATEGORIES, "all"], default=[])
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--bundle", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            result = analyze(args.project)
        elif args.command == "create":
            result = create(args.project, args.output, args.approve_overwrite, args.include_category)
        else:
            result = verify(args.bundle)
        print(json.dumps(result, sort_keys=True))
    except (checkpoint.CheckpointError, OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        print(f"project-checkpoint-portable: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
