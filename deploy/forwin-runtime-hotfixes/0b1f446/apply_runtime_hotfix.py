from __future__ import annotations

import argparse
import hashlib
import py_compile
from pathlib import Path


APP_ROOT = Path("/app")

# These are the exact files in forwin-forwin:compat-3738779.  Verify every
# pre-image before changing anything so a different compatibility layer fails
# closed rather than silently receiving a partial hotfix.
EXPECTED_BASE_SHA256 = {
    "forwin/canon/admission.py": "80fd1add05cb1a6e96cac4775b1baeeb644878348a2dcd72394bff9df340e768",
    "forwin/application/projects/reviews.py": "121f436557880a9a90249227b9552e4b52efc658f8eba13189238a117dc64832",
    "forwin/book_state/repository.py": "1a187d416403568eeb54791c617595f604419cb527586a38f27d230c5dc97e04",
}

# The changed files above are byte-identical in the immediately preceding
# compat-a6082199361c layer.  These two untouched runtime sentinels identify
# compat-3738779 itself and make an attempted lower-base build fail closed.
EXPECTED_COMPATIBILITY_SENTINEL_SHA256 = {
    "forwin/context/assembler_core/canon_quality_context.py": "9c8520d3f7e6b0849a252e34250d6e4362cc5c786e7be641cd6f5b88a6e73050",
    "forwin/writer/prompt_core/constraints.py": "196b659a6e04f049c6372a721a462cae7860be5f4103f80a4c7ef3b2f9259c84",
}

# The staged sources are copied from source commit
# 0b1f44656613b00fea01d4cf6bb34f1e4cfd673b.
EXPECTED_SOURCE_SHA256 = {
    "forwin/canon/historical_rewrite.py": "fca0286aa4a80820042a1431cee008be7b2086d39cfe076f154f25ebe66ee8b1",
    "forwin/canon/admission.py": "689fc2f7a74b89d6bb64bc34aff35706b90d3d2244a8f80dd35f44171326b880",
    "forwin/application/projects/reviews.py": "a9f800fe01b588af1952e9550eadd551f8affc56536763e06abf530d39ebbc92",
    "forwin/book_state/repository.py": "91b0a5d3c911c430586fa31955e666611a31e96f81149a2dee2cf330f789b4cc",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_sha256(path: Path, expected: str, *, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing {label}: {path}")
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(
            f"unexpected {label} for {path}: expected {expected}, got {actual}"
        )


def assert_absent(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"unexpected existing runtime target: {path}")


def install_exact_source(*, source_root: Path, relative_path: str) -> Path:
    source = source_root / relative_path
    target = APP_ROOT / relative_path
    expected = EXPECTED_SOURCE_SHA256[relative_path]
    assert_sha256(source, expected, label="staged hotfix source")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    assert_sha256(target, expected, label="installed hotfix source")
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args()
    source_root = args.source_root
    if not source_root.is_dir():
        raise RuntimeError(f"missing staged hotfix source root: {source_root}")

    # Verify every existing target and compatibility sentinel first.  Do not
    # reorder this ahead of the writes: a mismatch must leave the image
    # filesystem intact.
    for relative_path, expected in EXPECTED_BASE_SHA256.items():
        assert_sha256(APP_ROOT / relative_path, expected, label="runtime base")
    for relative_path, expected in EXPECTED_COMPATIBILITY_SENTINEL_SHA256.items():
        assert_sha256(APP_ROOT / relative_path, expected, label="compatibility base")
    assert_absent(APP_ROOT / "forwin/canon/historical_rewrite.py")

    targets = [
        install_exact_source(source_root=source_root, relative_path=relative_path)
        for relative_path in EXPECTED_SOURCE_SHA256
    ]
    for target in targets:
        py_compile.compile(str(target), doraise=True)


if __name__ == "__main__":
    main()
