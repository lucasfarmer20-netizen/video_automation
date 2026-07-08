"""Single orchestrator entrypoint.

Routes to modular code in ``src/`` and enforces the hard human-approval gate:
the pipeline pauses after draft-image generation, launches the Flask dashboard,
and refuses to call any paid video API until an approved
``storyboard_manifest.json`` has been written.

See CLAUDE.md for the binding codebase rules.
"""

# TODO: implement orchestration and the human-approval gate.
