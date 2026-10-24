#!/usr/bin/env bash
# Install one MatrAIx chat task per FixCam sample case into a MatrAIx checkout.
#   bash eval/matraix/install.sh /path/to/MatrAIx-Persona-8B
set -euo pipefail
MATRAIX="${1:?usage: install.sh /path/to/MatrAIx-Persona-8B}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SAMPLES="$(cd "$HERE/../../samples" && pwd)"
EXAMPLE="$MATRAIX/application/tasks/example-chat-api_support_chatbot"
[ -d "$EXAMPLE" ] || { echo "not a MatrAIx checkout: $MATRAIX" >&2; exit 1; }

for case_dir in "$SAMPLES"/*/; do
  case_id="$(basename "$case_dir")"
  [ -f "$case_dir/case.md" ] || { echo "skip $case_id (no case.md)"; continue; }
  task="$MATRAIX/application/tasks/fixcam-chat_${case_id}"
  env="$MATRAIX/environment/task-environments/application/chatbot-api-sidecar_fixcam_${case_id}"

  rm -rf "$task" "$env"
  mkdir -p "$task/input" "$env"
  cp -R "$EXAMPLE/tests" "$EXAMPLE/solution" "$task/"
  cp "$EXAMPLE/input/self_report_schema.yaml" "$EXAMPLE/persona_strategy.json" "$EXAMPLE/reporting.json" "$task/"
  mv "$task/self_report_schema.yaml" "$task/input/"
  cp "$HERE/task/instruction.md" "$task/"
  cp "$HERE/task/input/chatbot.yaml" "$HERE/task/input/protocol.md" "$task/input/"
  cp "$case_dir/case.md" "$task/input/context.md"
  sed -e "s/application\/fixcam-chat\"/application\/fixcam-chat_${case_id}\"/" \
      -e "s/chatbot-api-sidecar_fixcam\"/chatbot-api-sidecar_fixcam_${case_id}\"/" \
      "$HERE/task/task.toml" > "$task/task.toml"
  # Oracle solution talks to our sidecar host name instead of the Acme one.
  sed -i.bak -e 's#http://support-api:8000#http://fixcam-api:8000#' \
      -e 's#-H "Content-Type: application/json"#--max-time 300 -H "Content-Type: application/json"#' \
      "$task/solution/solve.sh" && rm -f "$task/solution/solve.sh.bak"

  cp -R "$HERE/env/fixcam-proxy" "$env/"
  sed "s/__CASE_ID__/${case_id}/" "$HERE/env/docker-compose.yaml.tmpl" > "$env/docker-compose.yaml"
  echo "installed fixcam-chat_${case_id}"
done
