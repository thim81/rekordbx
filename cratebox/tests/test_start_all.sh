#!/bin/sh
set -eu

cratebox_dir=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
test_directory=$(mktemp -d)
trap 'rm -rf "$test_directory"' EXIT INT TERM
mkdir -p "$test_directory/bin"

cat >"$test_directory/bin/uv" <<'SH'
#!/bin/sh
printf '%s\n' "$@" > "$TEST_RECORD/uv.tmp"
mv "$TEST_RECORD/uv.tmp" "$TEST_RECORD/uv.args"
SH
cat >"$test_directory/bin/npm" <<'SH'
#!/bin/sh
printf '%s\n' "$@" > "$TEST_RECORD/npm.args"
# Keep the mock UI alive until the background API has recorded its arguments.
attempts=0
while [ ! -f "$TEST_RECORD/uv.args" ] && [ "$attempts" -lt 50 ]; do
  attempts=$((attempts + 1))
  sleep 0.02
done
SH
chmod +x "$test_directory/bin/uv" "$test_directory/bin/npm"

PATH="$test_directory/bin:$PATH" TEST_RECORD="$test_directory" \
  sh "$cratebox_dir/scripts/start-all.sh"

grep -q '^preview$' "$test_directory/npm.args"
grep -q '^--port$' "$test_directory/npm.args"
grep -q '^4444$' "$test_directory/npm.args"
grep -q '^--host$' "$test_directory/npm.args"
grep -q '^127.0.0.1$' "$test_directory/npm.args"
grep -q '^server:app$' "$test_directory/uv.args"
grep -q '^--port$' "$test_directory/uv.args"
grep -q '^8000$' "$test_directory/uv.args"
grep -q '^onnxruntime$' "$test_directory/uv.args"
grep -q '^opencv-python-headless$' "$test_directory/uv.args"
if grep -q -- '--reload' "$test_directory/uv.args"; then
  echo 'production API command must not enable reload' >&2
  exit 1
fi
