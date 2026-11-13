"""Set the release version everywhere the release workflow checks it.

    python scripts/bump_version.py 0.9.8

Updates pyproject.toml, extensions/vscode/package.json and its
package-lock.json (both of its version fields), and the desktop app's
package.json, tauri.conf.json and Cargo.toml. The release workflow refuses
a tag vX.Y.Z unless pyproject.toml and package.json both say X.Y.Z, so bump
with this script, commit, then tag vX.Y.Z.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(version: str) -> int:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"not a version: {version!r} (expected X.Y.Z, without the v)", file=sys.stderr)
        return 1

    pyproject = ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    text, n = re.subn(r'(?m)^version = "[^"]*"', f'version = "{version}"', text, count=1)
    if n != 1:
        print("no version line in pyproject.toml", file=sys.stderr)
        return 1
    pyproject.write_text(text, encoding="utf-8")

    # Only the version values change: the files keep their own formatting.
    package = ROOT / "extensions" / "vscode" / "package.json"
    text = package.read_text(encoding="utf-8")
    text = re.sub(r'^(  "version": )"[^"]*"', rf'\g<1>"{version}"', text, count=1, flags=re.M)
    package.write_text(text, encoding="utf-8")

    lock = ROOT / "extensions" / "vscode" / "package-lock.json"
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["version"] = version
    data.setdefault("packages", {}).setdefault("", {})["version"] = version
    lock.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # The desktop app (desktop/): its package.json, Tauri config and crate.
    desktop = ROOT / "desktop"
    desktop_package = desktop / "package.json"
    text = desktop_package.read_text(encoding="utf-8")
    text = re.sub(r'^(  "version": )"[^"]*"', rf'\g<1>"{version}"', text, count=1, flags=re.M)
    desktop_package.write_text(text, encoding="utf-8")
    tauri_conf = desktop / "src-tauri" / "tauri.conf.json"
    text = tauri_conf.read_text(encoding="utf-8")
    text = re.sub(r'^(  "version": )"[^"]*"', rf'\g<1>"{version}"', text, count=1, flags=re.M)
    tauri_conf.write_text(text, encoding="utf-8")
    cargo = desktop / "src-tauri" / "Cargo.toml"
    text = cargo.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^version = "[^"]*"', f'version = "{version}"', text, count=1)
    cargo.write_text(text, encoding="utf-8")

    for path in (pyproject, package, lock, desktop_package, tauri_conf, cargo):
        print(f"{path.relative_to(ROOT)}: {version}")
    print(f"next: git commit -am 'chore(release): {version}' && git tag v{version} && git push origin main v{version}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1].removeprefix("v")))
