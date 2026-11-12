"""Download the tree-sitter grammars the parsers use, retrying on network errors.

tree-sitter-language-pack fetches each grammar from GitHub releases the first
time it is loaded. In CI that happens in the middle of the tests, where one
5xx from GitHub fails an unrelated test. Run this once before the tests (the
workflows also cache the result between runs):

    python scripts/prefetch_grammars.py

The cache goes where TREE_SITTER_LANGUAGE_PACK_CACHE_DIR points, else to the
language pack's default (under the user's cache folder).
"""
import sys
import time

# Grammars loaded by the parsers and the test suite (pack names: c_sharp is "csharp").
LANGUAGES = [
    "c", "cpp", "csharp", "css", "dart", "elisp", "elixir", "go", "haskell", "hcl",
    "html", "java", "javascript", "kotlin", "lua", "perl", "php", "python", "ruby",
    "rust", "scala", "solidity", "svelte", "swift", "tsx", "typescript", "vue",
]
ATTEMPTS = 6
DELAY_SECONDS = 15


def main() -> int:
    import tree_sitter_language_pack as pack

    for attempt in range(1, ATTEMPTS + 1):
        try:
            pack.prefetch(LANGUAGES)
        except Exception as exc:  # noqa: BLE001 - network errors are what we retry
            print(f"attempt {attempt}/{ATTEMPTS} failed: {exc}", file=sys.stderr)
            if attempt == ATTEMPTS:
                return 1
            time.sleep(DELAY_SECONDS * attempt)
        else:
            print(f"{len(LANGUAGES)} grammars ready in {pack.cache_dir()}")
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
