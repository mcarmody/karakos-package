import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()


def test_bake_step_and_env_share_cache_path():
    env = re.search(r"^ENV FASTEMBED_CACHE_PATH=(\S+)", DOCKERFILE, re.M)
    assert env, "ENV FASTEMBED_CACHE_PATH missing"
    bake = re.search(r"TextEmbedding\('BAAI/bge-small-en-v1\.5', cache_dir='([^']+)'\)", DOCKERFILE)
    assert bake, "model bake step missing"
    assert bake.group(1) == env.group(1)


def test_bake_runs_after_pip_install_and_is_skippable():
    assert DOCKERFILE.index("pip install --no-cache-dir -r requirements.txt") \
        < DOCKERFILE.index("TextEmbedding(")
    assert re.search(r"^ARG KARAKOS_BAKE_EMBED_MODEL=1", DOCKERFILE, re.M)
    assert '"$KARAKOS_BAKE_EMBED_MODEL" = "1"' in DOCKERFILE
