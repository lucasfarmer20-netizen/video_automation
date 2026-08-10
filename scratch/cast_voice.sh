#!/usr/bin/env bash
# Promote the chosen designed voice, then point MichaelHeney at it.
#
# Exists because pasting these as inline curl commands kept failing: the terminal
# hard-wraps at roughly 100 characters, and a wrap lands either inside a JSON
# string (invalid control character) or before the URL (curl gets no URL and bash
# tries to execute the address). A committed script has nothing long to paste.
#
#   export KEY=...        # STUDIO_API_KEY
#   bash scratch/cast_voice.sh
set -euo pipefail

BASE="${BASE:-https://youtube-video-pipeline-mfelaj54qa-uc.a.run.app}"
: "${KEY:?Set KEY first:  KEY=\$(gcloud secrets versions access latest --secret=STUDIO_API_KEY)}"

HERE="$(cd "$(dirname "$0")" && pwd)"
JSON="Content-Type: application/json"

echo "== promoting the designed preview into a real voice"
PROMOTE=$(curl -sS -X POST -H "X-Studio-Key: $KEY" -H "$JSON" \
  -d @"$HERE/promote_voice.json" "$BASE/api/casting/promote")
echo "$PROMOTE"

# A saved voice is what makes the rest meaningful; assigning a profile whose
# voice_id is empty would leave narration silently on the old narrator.
case "$PROMOTE" in
  *'"ok":true'*) ;;
  *) echo "!! promote failed — not assigning."; exit 1 ;;
esac

echo
echo "== assigning the profile to the active project"
curl -sS -X POST -H "X-Studio-Key: $KEY" -H "$JSON" \
  -d @"$HERE/assign_voice.json" "$BASE/api/casting/assign"
echo
echo
echo "== what this project now narrates with"
curl -sS "$BASE/api/casting/profiles"
echo
