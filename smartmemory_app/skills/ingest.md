# /ingest

Ingest a block of text or the current file into SmartMemory.

When the user invokes `/ingest`, take the provided text block, pasted content, or current file content and call `memory_save`. Report the item_id and the save status (`saved`, `queued`, `held` or `failed`).

**Usage:** `/ingest` (with pasted text or current file) or `/ingest <text>`

**Tool call:** `memory_save(content="<content>", memory_type="episodic")`
