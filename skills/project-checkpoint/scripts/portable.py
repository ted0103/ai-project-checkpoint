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

SCHEMA = 4
MAX_ENTRIES = 20_000
MAX_MANIFEST = 16 * 1024 * 1024
MAX_GUIDE = 64 * 1024
RESERVED = {".project-checkpoint/START_HERE.md", ".project-checkpoint/bundle.json"}
CATEGORIES = (
    "design-ui", "runtime-assets", "source", "config", "tests", "docs", "data",
    "runtime-vendor", "other", "assets", "vendor", "release",
)
PROFILES = {
    "ui": {"design-ui"},
    "runnable": {
        "design-ui", "runtime-assets", "source", "config", "tests", "docs", "data",
        "runtime-vendor", "other",
    },
    "all": set(CATEGORIES),
}
ARCHIVE_SUFFIXES = {".7z", ".apk", ".bz2", ".dmg", ".gz", ".ipa", ".rar", ".tar", ".tgz", ".xz", ".zip"}
DESIGN_UI_SUFFIXES = {
    ".astro", ".css", ".htm", ".html", ".jsx", ".less", ".sass", ".scss", ".styl",
    ".svelte", ".svg", ".tsx", ".vue",
}
ASSET_SUFFIXES = {
    ".avif", ".gif", ".heic", ".ico", ".jpeg", ".jpg", ".mp3", ".mp4", ".otf",
    ".pdf", ".png", ".ttf", ".webm", ".webp", ".woff", ".woff2",
}
SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".kt", ".m",
    ".mm", ".php", ".py", ".rb", ".rs", ".sh", ".swift", ".ts",
}
DATA_SUFFIXES = {".csv", ".json", ".jsonl", ".ndjson", ".sql", ".tsv", ".xml", ".yaml", ".yml"}
DOC_SUFFIXES = {".adoc", ".md", ".mdx", ".rst", ".txt"}
UI_DIRS = {"components", "design", "layouts", "pages", "styles", "ui", "views"}
NON_RUNTIME_ASSET_DIRS = {
    "archive", "archives", "design", "docs", "examples", "generated-source", "proof",
    "proofs", "qa", "reference", "references", "release", "releases", "screenshots",
}
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
    "id_ecdsa", "id_rsa", "service-account.json", "service_account.json",
    "application_default_credentials.json", "secrets.json", "secrets.yaml", "secrets.yml",
}
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
SECRET_BYTES = [
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\b(?:ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|xox[baprs]-[0-9A-Za-z-]{10,}|ya29\.[0-9A-Za-z_-]{20,})\b"),
    re.compile(rb"(?i)https?://[^\s/@:]+:[^\s/@]+@"),
    re.compile(rb"(?i)(?:^|\n)\s*//[^\n:]+:_authToken\s*=\s*[^\s$][^\s]*"),
]
GENERIC_SECRET_BYTES = re.compile(
    rb"""(?ix)
    (?:^|[\n,{])\s*["']?
    (?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|
       secret[_-]?(?:access[_-]?)?key|password|passwd|private[_-]?key)
    ["']?\s*[:=]\s*["']?([^"'\s,}]{8,})
    """
)
PLACEHOLDER_SECRET_BYTES = re.compile(
    rb"(?i)^(?:\$\{?[A-Z_][A-Z0-9_]*\}?|your[_-].*|(?:example|placeholder|dummy|sample|redacted|changeme|replace[_-]?me)(?:[_-].*)?|.*[_-](?:example|placeholder|dummy|sample|redacted)(?:[_-].*)?)$"
)


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
    top = path.parts[0].lower()
    name = path.name.lower()
    suffix = path.suffix.lower()
    non_runtime_asset = (
        top in NON_RUNTIME_ASSET_DIRS
        or bool(parts & {"archive", "archives", "generated-source", "proof", "proofs", "qa", "screenshots"})
    )
    if "release" in parts or "releases" in parts or suffix in ARCHIVE_SUFFIXES:
        return "release"
    if "vendor" in parts or "third_party" in parts or "third-party" in parts:
        return "vendor" if non_runtime_asset else "runtime-vendor"
    if (
        "test" in parts
        or "tests" in parts
        or "spec" in parts
        or "specs" in parts
        or (suffix in SOURCE_SUFFIXES | {".cjs", ".mjs"} and re.search(r"(?:^test[._-]|[._-](?:test|spec)\.[^.]+$)", name))
    ):
        return "tests"
    if suffix in ASSET_SUFFIXES or parts & {"assets", "fonts", "icons", "images"}:
        return "assets" if non_runtime_asset else "runtime-assets"
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


def selected_paths(paths, required, dirty, profile, include_categories):
    if profile not in PROFILES:
        fail(f"Unknown bundle profile: {profile}")
    include = set(CATEGORIES) if "all" in include_categories else PROFILES[profile] | set(include_categories)
    selected = []
    omitted = set()
    actual = set()
    for raw in paths:
        rel = safe_relative(raw)
        category = classify_path(rel)
        active_progress = profile == "runnable" and rel in dirty and category != "release"
        if rel in required or category in include or active_progress:
            selected.append(raw)
            if rel not in required:
                actual.add(category)
        else:
            omitted.add(category)
    return selected, sorted(omitted), sorted(actual)


def validate_selected_symlinks(root, visible, selected):
    visible_by_name = {safe_relative(raw): raw for raw in visible}
    selected_names = {safe_relative(raw) for raw in selected}
    for rel in sorted(selected_names):
        native = os.path.join(os.fsencode(root), os.fsencode(rel))
        try:
            info = os.lstat(native)
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(info.st_mode):
            continue
        target_text = validate_symlink_target(rel, os.fsencode(os.readlink(native)))
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target_text))
        target_native = os.path.join(os.fsencode(root), os.fsencode(resolved))
        if not os.path.lexists(target_native):
            fail(f"Dangling symlink refused: {rel} -> {target_text}")
        if os.path.isdir(target_native):
            target_prefix = resolved.rstrip("/") + "/"
            target_members = {name for name in visible_by_name if name.startswith(target_prefix)}
            if not target_members:
                fail(f"Symlink directory target has no Git-visible files: {rel} -> {target_text}")
            missing = target_members - selected_names
        else:
            missing = {resolved} - selected_names
        if missing:
            sample = ", ".join(sorted(missing)[:3])
            fail(f"Symlink target omitted by the selected profile: {rel} -> {target_text}; include {sample}.")


def source_paths(root, meta, profile, include_categories):
    paths, required = visible_paths(root, meta)
    dirty = {safe_relative(entry[0]) for entry in checkpoint.status_entries(root)}
    selected, omitted, actual = selected_paths(paths, required, dirty, profile, include_categories)
    validate_selected_symlinks(root, paths, selected)
    return selected, required, omitted, actual


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
    dirty = {safe_relative(entry[0]) for entry in checkpoint.status_entries(root)}
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
    profiles = {}
    for profile in PROFILES:
        selected, _, actual = selected_paths(raw_paths, required, dirty, profile, [])
        sizes = [path_size(root, raw) for raw in selected]
        profiles[profile] = {
            "categories": actual,
            "file_count": sum(size is not None for size in sizes),
            "source_bytes": sum(size for size in sizes if size is not None),
        }
    return {
        "project": root.name,
        "default_profile": "runnable",
        "required": required_summary,
        "categories": groups,
        "profiles": profiles,
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
    for match in GENERIC_SECRET_BYTES.finditer(sample):
        candidate = match.group(1).rstrip(b";")
        if not PLACEHOLDER_SECRET_BYTES.fullmatch(candidate):
            fail(f"Potential credential assignment detected in {path}; remove or redact it before bundling.")
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


def start_here(project, schema=SCHEMA, profile=None, included_categories=None):
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
    if schema == 2:
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
    if schema == 3:
        return (
            "# Portable Project Checkpoint\n\n"
            "Open `HANDOFF.md` first. Follow its exact next action and reuse only the setup, run, and "
            "verification commands recorded there or documented in the included project files.\n\n"
            "This archive contains the minimum runnable working set: active non-release progress, source, "
            "configuration, tests, documentation, data, runtime assets, runtime vendor files, and checkpoint "
            "references. Large design/reference assets and release artifacts may be intentionally absent; "
            "check `.project-checkpoint/bundle.json` before assuming they exist. `.git`, Git history, deleted "
            "paths, and Git-ignored files are absent. Do not claim saved verification is current after editing.\n\n"
            f"Project directory: `{project}`\n"
        ).encode()
    categories = ", ".join(included_categories or []) or "checkpoint references only"
    if profile == "ui":
        scope = (
            "This is a UI/style working set, not a runnable-project guarantee. Source, configuration, tests, "
            "assets, vendor files, or release files may be absent unless listed in the manifest."
        )
    elif profile == "all":
        scope = (
            "This archive contains every Git-visible file plus checkpoint references. Git history, deleted "
            "paths, and unreferenced ignored files remain absent."
        )
    else:
        scope = (
            "This archive contains the selected runnable working set: active non-release progress and the "
            "source, configuration, tests, documentation, data, runtime assets, runtime vendor files, and "
            "supporting files present in the manifest. Large reference assets and releases may be absent."
        )
    return (
        "# Portable Project Checkpoint\n\n"
        "Open `HANDOFF.md` first. Follow its exact next action and reuse only the setup, run, and "
        "verification commands recorded there or documented in the included project files.\n\n"
        f"{scope}\n\n"
        f"Profile: `{profile}`. Included categories: {categories}.\n\n"
        "Verification proves archive self-consistency, not who created it or whether its code is safe to "
        "execute. `.git`, Git history, deleted paths, and unreferenced ignored files are absent. Do not claim "
        "saved verification is current after editing.\n\n"
        f"Project directory: `{project}`\n"
    ).encode()


def create(project, output, approved=False, include_categories=None, profile="runnable"):
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
    raw_paths, required, omitted_categories, included_categories = source_paths(
        root, meta, profile, include_categories,
    )
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
            guide = start_here(prefix, SCHEMA, profile, included_categories)
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
                    "Git-ignored files except explicit checkpoint references",
                    *([f"Git-visible categories not selected: {', '.join(omitted_categories)}"] if omitted_categories else []),
                ],
            }
            if SCHEMA >= 4:
                manifest.update({"profile": profile, "included_categories": included_categories})
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
        "profile": profile,
        "included_categories": included_categories,
        "output": os.fspath(output),
    })
    return result


def validate_zip_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or "\\" in name or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"Unsafe archive member: {name}")
    return path


def read_zip_bounded(zf, name, limit, label):
    try:
        info = zf.getinfo(name)
    except KeyError:
        fail(f"Bundle {label} is missing.")
    if info.file_size > limit:
        fail(f"Bundle {label} exceeds the {limit}-byte limit.")
    data = bytearray()
    with zf.open(info) as source:
        while len(data) <= limit:
            chunk = source.read(min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    if len(data) > limit:
        fail(f"Bundle {label} exceeds the {limit}-byte limit.")
    return bytes(data)


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
        manifest_raw = read_zip_bounded(zf, manifests[0], MAX_MANIFEST, "manifest")
        try:
            manifest = json.loads(manifest_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise checkpoint.CheckpointError("Bundle manifest is invalid JSON.") from e
        if canonical(manifest) != manifest_raw:
            fail("Bundle manifest is not canonical.")
        legacy_keys = {"schema", "created_at", "project", "branch", "commit", "fingerprint", "handoff", "files", "omitted"}
        schema = manifest.get("schema") if isinstance(manifest, dict) else None
        required_keys = legacy_keys | ({"profile", "included_categories"} if type(schema) is int and schema >= 4 else set())
        if not isinstance(manifest, dict) or set(manifest) != required_keys or type(schema) is not int or schema not in {1, 2, 3, SCHEMA}:
            fail("Bundle manifest schema is invalid.")
        if (
            not all(isinstance(manifest[key], str) for key in ("created_at", "project", "branch", "commit", "fingerprint", "handoff"))
            or not isinstance(manifest["files"], list)
            or not isinstance(manifest["omitted"], list)
            or not all(isinstance(value, str) for value in manifest["omitted"])
        ):
            fail("Bundle manifest field types are invalid.")
        if schema >= 4 and (
            not isinstance(manifest["profile"], str)
            or manifest["profile"] not in PROFILES
            or not isinstance(manifest["included_categories"], list)
            or not all(isinstance(category, str) for category in manifest["included_categories"])
            or manifest["included_categories"] != sorted(set(manifest["included_categories"]))
            or not all(category in CATEGORIES for category in manifest["included_categories"])
        ):
            fail("Bundle profile metadata is invalid.")
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
        guide_name = f"{prefix}/.project-checkpoint/START_HERE.md"
        guide = read_zip_bounded(zf, guide_name, MAX_GUIDE, "resume instructions")
        expected_guide = start_here(
            prefix, schema, manifest.get("profile"), manifest.get("included_categories"),
        )
        if guide != expected_guide:
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
            handoff_raw = read_zip_bounded(zf, handoff_name, checkpoint.MAX_HANDOFF, "HANDOFF.md")
            handoff_meta = checkpoint.parse_handoff(handoff_raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise checkpoint.CheckpointError("Bundled HANDOFF.md is not UTF-8.") from e
        if any(manifest[key] != handoff_meta[key] for key in ("branch", "commit", "fingerprint")):
            fail("Bundle Git identity does not match HANDOFF.md.")
        if schema >= 4:
            required_files = {"HANDOFF.md", *handoff_meta["references"]}
            if handoff_meta.get("plan"):
                required_files.add(handoff_meta["plan"])
            actual_categories = sorted({classify_path(rel) for rel in listed - required_files})
            if manifest["included_categories"] != actual_categories:
                fail("Bundle included categories do not match its file manifest.")
    result = {
        "verified": True,
        "bundle": os.fspath(bundle),
        "project": prefix,
        "branch": manifest["branch"],
        "commit": manifest["commit"],
        "file_count": len(manifest["files"]),
        "source_bytes": total,
        "sha256": archive_digest.hexdigest(),
    }
    if schema >= 4:
        result.update({
            "profile": manifest["profile"],
            "included_categories": manifest["included_categories"],
        })
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    analyze_parser = commands.add_parser("analyze")
    analyze_parser.add_argument("--project", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--project", required=True)
    create_parser.add_argument("--output", required=True)
    create_parser.add_argument("--approve-overwrite", action="store_true")
    create_parser.add_argument("--profile", choices=sorted(PROFILES), default="runnable")
    create_parser.add_argument("--include-category", action="append", choices=[*CATEGORIES, "all"], default=[])
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--bundle", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            result = analyze(args.project)
        elif args.command == "create":
            result = create(
                args.project, args.output, args.approve_overwrite, args.include_category, args.profile,
            )
        else:
            result = verify(args.bundle)
        print(json.dumps(result, sort_keys=True))
    except (checkpoint.CheckpointError, OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        print(f"project-checkpoint-portable: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
