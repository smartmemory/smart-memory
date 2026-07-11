# SmartMemory

**Give your AI a memory.** Your coding assistant forgets everything the moment a session ends. SmartMemory fixes that.

[![Docs](https://img.shields.io/badge/docs-smartmemory.ai-blue)](https://docs.smartmemory.ai/)
[![PyPI version](https://badge.fury.io/py/smartmemory.svg)](https://pypi.org/project/smartmemory/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**[Read the docs](https://docs.smartmemory.ai/)**

SmartMemory remembers what you and your AI learn while you work: decisions, preferences, facts, and hard-won lessons. Next session, the relevant parts come back automatically. It runs on your machine by default, with no Docker and no database to install.

Works out of the box with **Claude Code**, **Cursor**, and other MCP-compatible tools, or directly from **Python**.

## Get started in 2 minutes

```bash
pip install smartmemory
smartmemory setup
```

That's it. Setup asks a couple of questions (arrow keys, sensible defaults) and wires SmartMemory into Claude Code. Using Cursor instead? Run `smartmemory setup --for cursor`.

Now try it:

```bash
sm add "We chose Postgres over MongoDB because of the reporting queries"
sm add "Deploys go through GitHub Actions, never manual"
sm search "which database did we pick"
```

`sm` is the short alias for `smartmemory`. Every command works with either.

### What you get from here

- **Your AI remembers across sessions.** Setup installs hooks that capture context while you work and recall the relevant parts when the next session starts. No more re-explaining your project every morning.
- **Ask questions in plain language.** `sm search` is semantic search. "Which database did we pick" finds the Postgres memory even though the words don't match.
- **A knowledge graph you can see.** Everything you add gets linked into a graph you can explore in your browser. The tour below shows you around.

## A quick tour

Run the guided tour first:

```bash
sm tour
```

It starts an isolated local tour store, opens the graph viewer, seeds a few project facts, searches them, shows a real token receipt, then proves cross-session recall. The tour does not touch your real memories. Use `sm tour --no-viewer` for headless runs, or `sm tour --keep` if you want to inspect the temporary store afterward.

Prefer to read the flow instead? The written analog is below.

### 1. Capture things worth remembering

Decisions, gotchas, preferences. Add them as they happen:

```bash
sm add "Staging rate-limits at 100 requests per minute, batch the uploads"
sm add --type procedural "Reset the local DB with docker compose down -v, then ./dev.sh start"
```

Memory types (`semantic`, `procedural`, and more) are optional. Skip the flag and new memories land as `episodic`, the type for things that happened.

### 2. Feed it what you already have

Pipe in notes and documents instead of retyping them:

```bash
sm add - < meeting-notes.txt        # one memory per line
sm add --all - < project-brief.md   # the whole file as one memory
```

### 3. Keep projects separate

Tag memories with any property, then filter searches on it:

```bash
sm add --project atlas "Atlas v2 ships on the 15th"
sm search --project atlas "when do we ship"
sm search --top-k 20 "*"            # list more of everything
```

### 4. Meet the daemon

SmartMemory runs a small background daemon so the CLI answers in under 200ms instead of cold-starting Python on every command. Setup starts it for you, and on macOS it comes back after login and crashes.

```bash
sm status     # daemon health, memory count, enrichment queue
sm restart    # if you ever need a fresh start
```

The daemon is also where the quality comes from. `sm add` returns instantly because fast entity extraction runs in about 4ms. If you have an LLM API key configured, the daemon then quietly re-reads each memory in the background and adds the entities and relations the fast pass missed. You never wait for it, and `sm status` shows the queue draining.

### 5. Explore your knowledge graph

```bash
sm viewer
```

This opens an interactive graph in your browser. Every memory appears linked to the people, projects, and concepts inside it, and memories that share entities cluster together. After a few days of real use it reads like a map of your work. If the default port is taken, use `sm viewer --port 8080`.

## Use it with Claude Code

If you ran `smartmemory setup`, this is already working. Setup installs six hooks that follow the rhythm of a coding session:

- **Session starts**: SmartMemory recalls what it knows about the directory you're working in.
- **You send a prompt**: memories relevant to that prompt are injected as context.
- **Tools run**: tool calls are observed and captured.
- **A tool fails**: the error pattern is saved so the same mistake isn't repeated.
- **Claude finishes responding**: the response is paired with the prompt that produced it.
- **Session ends**: a session summary is persisted.

The net effect is that tomorrow's session starts where today's left off, without you pasting context around.

You also get slash commands inside Claude Code:

```
/remember <something worth keeping>
/search <query>
/ingest            (current file or a pasted block)
/orient            (what does SmartMemory know about this directory?)
```

Want Claude to call memory as a tool too? Install the MCP server:

```bash
sm mcp install claude-code    # also: cursor, codex
```

Using Cursor as your editor? `smartmemory setup --for cursor` configures it in one step.

## Use it with Obsidian

The [SmartMemory Obsidian plugin](https://github.com/smartmemory/smartmemory-obsidian) brings the same memory to your vault: every note becomes a structured memory with extracted entities, entity chips link every note that mentions the same person or project, a graph pane shows the neighborhood around the active note, and `Cmd+Shift+R` runs multi-hop semantic search across the whole vault. It can also propose `[[wikilinks]]` for entity mentions and warn you inline when a note contradicts something newer.

The plugin uses a SmartMemory account (free tier: 1,000 notes, 200 searches per day). Install it with [BRAT](https://github.com/TfTHacker/obsidian42-brat) by adding `smartmemory/smartmemory-obsidian`, then paste your API key from [app.smartmemory.ai](https://app.smartmemory.ai) into Settings → SmartMemory.

## What happens when you add a memory

SmartMemory doesn't just save text. It extracts the people, projects, and concepts inside each memory and links them into a knowledge graph. "Alice leads Project Atlas" becomes Alice, Project Atlas, and the relationship between them. That's why search can answer questions instead of just matching keywords, and why the viewer has a graph to draw.

## Use it from Python

```python
from smartmemory.tools.factory import create_lite_memory, lite_context

# Simple usage. Full LLM extraction runs if OPENAI_API_KEY is set.
memory = create_lite_memory()
item_id = memory.ingest("Alice leads Project Atlas")
results = memory.search("who leads Atlas", top_k=5)

# Preferred in scripts: cleans up globals and closes SQLite on exit.
with lite_context() as memory:
    item_id = memory.ingest("Alice leads Project Atlas")
    results = memory.search("who leads Atlas")

# Force no LLM calls (even if OPENAI_API_KEY is set).
from smartmemory.pipeline.config import PipelineConfig
memory = create_lite_memory(pipeline_profile=PipelineConfig.lite(llm_enabled=False))
```

## Everyday commands

```bash
sm add "text"                  # Remember something
sm search "query"              # Find memories by meaning
sm search "*"                  # List everything
sm viewer                      # Open the knowledge graph in your browser
sm status                      # Daemon health and memory count
sm config                      # View settings
sm clear                       # Start over (deletes all memories)
```

<details>
<summary><strong>Full command reference</strong></summary>

### Core

```bash
smartmemory add "text"                # Add a memory (default type: episodic)
smartmemory add --type semantic "..."  # Add with specific memory type
smartmemory add - < notes.txt          # Add from stdin, one memory per line
smartmemory add --all - < doc.txt      # Add entire stdin as one memory
smartmemory add --project atlas "..."  # Add with arbitrary property flags

smartmemory search "query"             # Semantic search
smartmemory search "*"                 # List all memories
smartmemory search --top-k 20 "query"  # Control result count (default: 5)
smartmemory search --project atlas "q" # Filter by property

smartmemory get <item_id>              # Fetch a single memory by ID
smartmemory retag --content "x" --origin seed:demo --dry-run  # Re-tag matched items' origin
smartmemory recall                     # Session context for Claude Code hooks
smartmemory recall --cwd /path         # Recall with working directory context
smartmemory viewer                     # Open knowledge graph viewer in browser
smartmemory viewer --port 8080         # Custom port for viewer
smartmemory models                     # List available LLM models
smartmemory config                     # View all settings
smartmemory config llm_provider        # View one setting
smartmemory config llm_provider groq   # Change a setting
smartmemory clear                      # Delete all memories and reset vectors
```

### Daemon

```bash
smartmemory start                      # Start daemon + enrichment workers
smartmemory stop                       # Stop daemon
smartmemory restart                    # Restart daemon
smartmemory status                     # Daemon health + enrichment stats
smartmemory worker                     # Run enrichment worker (drain and exit)
smartmemory worker --loop              # Run enrichment worker continuously
```

### Setup and lifecycle

```bash
smartmemory setup                      # Interactive first-run questionnaire
smartmemory setup --mode local         # Skip questionnaire, set local mode
smartmemory setup --mode remote --api-key sk_...  # Non-interactive remote setup
smartmemory setup --for cursor         # Configure for Cursor instead of Claude Code
smartmemory server                     # Start MCP server (called by MCP clients)
smartmemory uninstall                  # Remove hooks, skills, plist, and data
smartmemory uninstall --keep-data      # Remove hooks/plist but keep memories
```

### Admin

```bash
smartmemory export ./bundle            # Export memories to an OKF bundle directory (alias: admin export)
smartmemory import ./bundle            # Import an OKF bundle directory (alias: admin import)
smartmemory admin export out.jsonl --legacy-jsonl   # Legacy single-file JSONL corpus (read-during-migration)
smartmemory admin import data.jsonl --legacy-jsonl   # Import a legacy JSONL corpus
smartmemory admin reindex              # Re-embed all memories with current model
smartmemory admin reextract            # Re-run entity extraction on all memories
smartmemory admin list-packs           # List available seed packs
smartmemory admin install-pack NAME    # Install a seed pack
smartmemory admin mine                 # Mine Wikidata entities via SPARQL
smartmemory admin convert-rebel        # Convert REBEL dataset to corpus JSONL
```

### Code indexing and MCP

```bash
smartmemory code index <path>          # Index a code repo (AST entities + call graph) into memory
smartmemory mcp install claude-code    # Write MCP server config for a client (claude-code, cursor, codex)
```

### Lifecycle (hook-driven)

These are invoked automatically by the Claude Code hooks that `smartmemory setup` installs. You rarely call them by hand.

```bash
smartmemory lifecycle orient           # Recall context at session start
smartmemory lifecycle recall           # Inject prompt-relevant context
smartmemory lifecycle observe          # Capture a tool call
smartmemory lifecycle distill          # Pair a response with its stored prompt
smartmemory lifecycle learn            # Capture an error pattern
smartmemory lifecycle persist          # Save a session summary
smartmemory lifecycle status           # Show lifecycle config and session stats
```

</details>

## Install options

```bash
pip install smartmemory                  # Everything: local memory + MCP server + graph viewer + CLI
pip install smartmemory-core[lite]       # Core library only, no CLI/MCP/viewer
```

> **`smartmemory`** is the distribution package. A single install bundles `smartmemory-core[lite]` (local SQLite + usearch storage), the unified MCP server, the graph viewer, and the CLI. You pick **local** or **remote** mode at `smartmemory setup` time, not at install time.
> **`smartmemory-core`** is the core library for developers building on top of SmartMemory.

**Local mode** wires Claude Code hooks, downloads the spaCy language model (about 15MB), and starts a persistent daemon. **Remote mode** validates your API key and stores it in the OS keychain.

## Lite or Service

- **Lite** (the default): everything runs on your machine. SQLite graph plus usearch vectors, no Docker, no external services, no account. `pip install smartmemory` and go.
- **Service**: connect to the managed SmartMemory backend instead of running storage locally. Run `smartmemory setup --mode remote` and paste an API key from [app.smartmemory.ai](https://app.smartmemory.ai). Nothing to install or operate, and there is a free tier to start on.

You choose Lite or Service at `smartmemory setup` time, not at install time, and you can switch later by re-running setup.

## Going deeper

Everything below is here for the curious and for developers building on top of SmartMemory. You don't need any of it to use the tool.

### Memory types

SmartMemory sorts what it stores into 11 curated memory types. Five core types hold **knowledge** (what's true): Pending, Semantic, Episodic, Procedural, and Zettelkasten. The rest capture **expertise** (what to do and what not to do): Reasoning, Opinion, Observation, Decision, Constraint, and Learned.

The expertise layer is what makes an agent's memory useful for acting: captured choices, rejected alternatives, hard constraints, and lessons learned. Capture them with `mem.add_decision(...)`, `mem.add_constraint(...)`, and `mem.add_learning(...)`, and recall them with `mem.search(query, expertise=True)`. See [Expertise vs Knowledge](https://docs.smartmemory.ai/smartmemory/concepts/expertise-vs-knowledge) for the full mapping.

<details>
<summary><strong>All 11 memory types</strong></summary>

- **Pending Memory**: Short-term buffer for items awaiting consolidation (formerly "working", routed at ingest by the `ConsolidationRouter`)
- **Semantic Memory**: Facts and concepts with vector embeddings
- **Episodic Memory**: Personal experiences and learning history
- **Procedural Memory**: Skills, strategies, and learned patterns
- **Zettelkasten Memory**: Bidirectional note-taking system with AI-powered knowledge discovery
- **Reasoning Memory**: Chain-of-thought traces capturing "why" decisions were made (System 2)
- **Opinion Memory**: Beliefs with confidence scores, reinforced or contradicted over time
- **Observation Memory**: Synthesized entity summaries from scattered facts
- **Decision Memory**: First-class decisions with confidence tracking, provenance chains, and lifecycle management. Structured fields: `rejected_alternatives`, `rationale`, `constraints`. Capture: `mem.add_decision(...)`
- **Constraint Memory**: Hard rules, discovered or imposed. Capture: `mem.add_constraint(...)`
- **Learned Memory**: Lessons learned the hard way. Capture: `mem.add_learning(...)`

Structural types like `code`, `plan`, `evaluation`, `anchor`, and `tool_call` are used internally.

</details>

### Advanced recall in practice

The quickstart above is capture and simple search. These are the recall patterns a flat memory file cannot do.
Every example runs in local Lite mode with no API key (LLM extraction only matters when you want these
classified automatically out of raw prose, rather than recorded explicitly). Shared setup:

```python
from datetime import datetime, timezone
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import lite_context
from smartmemory.models.memory_item import MemoryItem

lite = dict(pipeline_profile=PipelineConfig.lite(llm_enabled=False))
```

**Decisions, including what you decided _not_ to do.** A decision is a first-class object that keeps the
rejected alternatives and the reasoning attached, so you can recall the road not taken.

```python
with lite_context(**lite) as m:
    d = m.add_decision(
        "Use SQLite for local lite storage",
        rejected_alternatives=["Postgres sidecar", "remote-only cloud sync"],
        rationale="Zero-infra installs must work offline",
    )
    print(m.get_decision(d.decision_id).rejected_alternatives)
    # -> ['Postgres sidecar', 'remote-only cloud sync']

    hits = m.search("what did we decide NOT to do for storage", expertise=True)
    print([i.content for i in hits["decision"]])
    # -> ['Use SQLite for local lite storage']
```

`search(expertise=True)` returns a dict keyed by expertise type (`decision`, `constraint`, `learned`, ...),
not a flat list.

**Truth maintenance: supersede a fact, and only the current one comes back.**

```python
with lite_context(**lite) as m:
    old = m.add(MemoryItem(content="The incident channel is #ops-old", memory_type="semantic"))
    new = m.add(MemoryItem(content="The incident channel is #ops-war-room", memory_type="semantic"))
    m.supersede(old, new, reason="Team renamed the channel")

    print([r.content for r in m.search("incident channel")])
    # -> ['The incident channel is #ops-war-room']                      (current only)
    print([r.content for r in m.search("incident channel", include_superseded=True)])
    # -> ['The incident channel is #ops-old', 'The incident channel is #ops-war-room']   (full history)
```

**Bi-temporal recall: what did we believe _before_ it changed.** Keep the old record's time with
`reference_time`, then ask as of a past moment. A markdown file cannot answer this: once you edit the line,
the old value is gone.

```python
with lite_context(**lite) as m:
    old = m.add(MemoryItem(content="The billing provider is Stripe", memory_type="semantic",
                           metadata={"reference_time": datetime.now(timezone.utc).isoformat()}))
    t_before_change = datetime.now(timezone.utc)
    new = m.add(MemoryItem(content="The billing provider is Paddle", memory_type="semantic",
                           metadata={"reference_time": datetime.now(timezone.utc).isoformat()}))
    m.supersede(old, new, reason="Pricing model changed")

    print([r.content for r in m.search("billing provider", as_of_date=t_before_change, include_superseded=True)])
    # -> ['The billing provider is Stripe']      (what you believed then)
    print([r.content for r in m.search("billing provider")])
    # -> ['The billing provider is Paddle']       (what you believe now)
```

**Constraints and lessons, recalled as expertise.**

```python
with lite_context(**lite) as m:
    m.add_constraint("Production deploys must use GitHub Actions", domain="release")
    m.add_learning("SQLite WAL avoids writer stalls under concurrent tests")
    hits = m.search("deploy and sqlite guidance", expertise=True)
    print([i.content for i in hits["constraint"]])
    # -> ['Production deploys must use GitHub Actions']
    print([i.content for i in hits["learned"]])
    # -> ['SQLite WAL avoids writer stalls under concurrent tests']
```

**Multi-hop retrieval** chains results so each hop informs the next query, reaching facts a single lookup would
miss. It pays off once your graph is rich (`semantic_hops=True` adds LLM-planned hops):

```python
with lite_context(**lite) as m:
    hits = m.search("Redis migration fallout", multi_hop=True, max_hops=3)
```

**Code intelligence: index a repo, then search it by meaning.** `sm code index` builds an AST and call graph
into memory. Recall it with `search_code`, which is semantic, so it finds code that shares no keywords with
your query (a plain `grep` would miss it):

```bash
sm code index ./my-project --repo my-project
```

```python
with lite_context(**lite) as m:
    # "clean up vendor billing records" matches a function actually named
    # reconcile_widget_invoice (no shared keywords, found by meaning).
    for hit in m.search_code("clean up vendor billing records", repo="my-project"):
        print(hit["name"], hit["entity_type"], f'{hit["file_path"]}:{hit["line_number"]}')
```

### The processing pipeline

`ingest()` runs an 11-stage pipeline:

```
classify -> coreference -> simplify -> entity_ruler -> llm_extract -> ontology_constrain -> store -> link -> enrich -> ground -> evolve
```

Each stage implements the `StageCommand` protocol (`execute(state, config) -> state`, `undo(state) -> state`). The pipeline supports breakpoint execution (`run_to()`, `run_from()`, `undo_to()`) for debugging and resumption.

`add()` is simple storage (normalize -> store -> embed) for internal or derived items.

When an LLM API key is available, the daemon runs **two-tier ingestion**:

- **Tier 1 (sync, ~4ms):** spaCy + EntityRuler extracts entities immediately and returns the item ID
- **Tier 2 (async, ~740ms):** A background drain thread runs LLM extraction and adds net-new entities and relations

This means `sm add` returns instantly while quality improves in the background.

### What makes it different

- **Self-learning EntityRuler**: Pattern-matching NER that improves with use. LLM discoveries feed back into rules (96.9% entity F1 at 4ms)
- **Memory evolution**: Built-in evolvers automatically transform memories over time, promoting stable facts, decaying stale ones, and strengthening what you actually use
- **Hybrid search**: Graph-structured search plus BM25/embedding RRF fusion with query decomposition for compound queries
- **Code indexer**: AST-based Python and TypeScript parser with cross-file call resolution, semantic code search, and memory-to-code graph bridging
- **Two-tier ingestion**: Instant spaCy extraction plus async LLM enrichment
- **MCP server**: Works with Claude Code, Cursor, and other MCP-compatible tools
- **Flexible scoping**: Optional `ScopeProvider` for multi-tenancy or unrestricted usage
- **Plugin security**: Sandboxing, permissions, and resource limits for safe plugin execution

<details>
<summary><strong>Memory evolution in detail</strong></summary>

SmartMemory includes built-in evolvers that automatically transform memories. In lite mode, evolution runs incrementally in the background.

**Core evolvers** (memory type transitions and lifecycle):
- **EpisodicToSemanticEvolver**: Promotes stable facts to semantic memory
- **EpisodicToZettelEvolver**: Converts episodic events to Zettelkasten notes
- **EpisodicDecayEvolver**: Archives old episodic memories
- **SemanticDecayEvolver**: Prunes low-relevance semantic facts
- **ZettelPruneEvolver**: Merges duplicate or low-quality notes
- **DecisionConfidenceEvolver**: Decays confidence on stale decisions, auto-retracts below threshold
- **OpinionSynthesisEvolver**: Synthesizes opinions from accumulated observations
- **ObservationSynthesisEvolver**: Creates entity summaries from scattered facts
- **OpinionReinforcementEvolver**: Adjusts opinion confidence based on new evidence
- **StaleMemoryEvolver**: Flags memories as stale when referenced source code changes

**Enhanced evolvers** (neuroscience-inspired dynamics):
- **ExponentialDecayEvolver**: Time-based activation decay with configurable half-life
- **RetrievalBasedStrengtheningEvolver**: Memories accessed more frequently become harder to forget
- **HebbianCoRetrievalEvolver**: Reinforces edges between memories retrieved together ("neurons that fire together wire together")
- **InterferenceBasedConsolidationEvolver**: Similar competing memories interfere, strengthening the dominant one
- **EnhancedWorkingToEpisodicEvolver**: Context-aware pending-to-episodic transition with richer metadata

**Sleep-cycle and agent evolvers** (opt-in background re-derivation, REM/NREM analogs):
- **MemoryConsolidationEvolver**: Re-derives consolidated memories across an entity's scattered facts during an idle "sleep cycle"
- **TemporalAgingEvolver**: Rewrites future/planned facts into resolved past tense once their date passes ("going to Singapore in July" becomes "went to Singapore in July 2026") via reversible bi-temporal supersession. Triggered by the passage of time, never by re-parsing prose
- **EvaluationEvolver**: Writes evidence-derived per-`(agent, dimension, domain)` performance scores via bi-temporal supersession. Read back with `get_evaluation()` / `list_evaluation_history()`
- **SemanticToProceduralEvolver** / **ProceduralReinforcementEvolver** / **AnchorReconciliationEvolver**: Procedural promotion, usage-based strengthening, and spec-anchor drift reconciliation

**Replaced by ConsolidationRouter (CORE-MEMORY-DYNAMICS-1 M1):**
- The former `WorkingToEpisodicEvolver` and `WorkingToProceduralEvolver` were retired. Routing from the `pending` bucket to `episodic` / `procedural` now happens at ingest time via the `ConsolidationRouter` pipeline stage.

</details>

<details>
<summary><strong>Plugin system in detail</strong></summary>

SmartMemory features a unified, extensible plugin architecture. All plugins follow a consistent class-based pattern.

**Auto-registered by default** (loaded by `PluginManager._load_builtin_plugins`):
- **Extractors**: `LLMExtractor`, `LLMSingleExtractor`, `ConversationAwareLLMExtractor` (always), plus `SpacyExtractor` and `GLiNER2Extractor` when their optional deps are importable
- **6 Enrichers**: `BasicEnricher`, `LinkExpansionEnricher`, `SentimentEnricher`, `TemporalEnricher`, `ExtractSkillsToolsEnricher`, `TopicEnricher`
- **9 Evolvers**: `EpisodicToSemanticEvolver`, `EpisodicDecayEvolver`, `SemanticDecayEvolver`, `EpisodicToZettelEvolver`, `ZettelPruneEvolver`, `ExponentialDecayEvolver`, `InterferenceBasedConsolidationEvolver`, `RetrievalBasedStrengtheningEvolver`, `EvaluationEvolver`
- **1 Grounder**: `WikipediaGrounder`

**Specialist plugins** (selected by pipeline stage, opt-in feature, or the Studio evolver catalog, not auto-run in the idle cycle):
- **Extractors**: `GroqExtractor`, `DecisionExtractor`, `ReasoningExtractor`
- **Enrichers**: `UsageTrackingEnricher`
- **Evolvers**: `DecisionConfidenceEvolver`, `OpinionSynthesisEvolver`, `ObservationSynthesisEvolver`, `OpinionReinforcementEvolver`, `StaleMemoryEvolver`, `HebbianCoRetrievalEvolver`, `MemoryConsolidationEvolver`, `TemporalAgingEvolver`, `SemanticToProceduralEvolver`, `ProceduralReinforcementEvolver`, `AnchorReconciliationEvolver`
- **Grounders**: `PublicKnowledgeGrounder` (Wikidata QIDs)

> Two registries coexist by design: a small **eager, auto-run** set (above) that runs in the idle/batch evolution cycle, and a **lazy catalog of ~40 evolvers** (`evolution/registry.py`) that Studio can select into workflow DAGs without importing every class at boot. Auto-run ⊆ catalog is an enforced invariant.

**Creating custom plugins:**

```python
from smartmemory.plugins.base import EnricherPlugin, PluginMetadata

class MyCustomEnricher(EnricherPlugin):
    @classmethod
    def metadata(cls):
        return PluginMetadata(
            name="my_enricher",
            version="1.0.0",
            author="Your Name",
            description="My custom enricher",
            plugin_type="enricher",
            dependencies=["some-lib>=1.0.0"],
            security_profile="standard",
            requires_network=False,
            requires_llm=False
        )

    def enrich(self, item, node_ids=None):
        item.metadata["custom_field"] = "value"
        return item.metadata
```

**Publishing plugins:**

```toml
# pyproject.toml
[project.entry-points."smartmemory.plugins.enrichers"]
my_enricher = "my_package:MyCustomEnricher"
```

```bash
pip install my-smartmemory-plugin
# Automatically discovered and loaded!
```

</details>

<details>
<summary><strong>Python API reference</strong></summary>

### SmartMemory class

```python
class SmartMemory:
    def __init__(
        self,
        scope_provider=None,
        vector_backend=None,
        cache=None,
        observability: bool = True,
        pipeline_profile=None,
        entity_ruler_patterns=None,
    )

    # Primary API
    def ingest(self, item, sync=True, **kwargs) -> str  # Full pipeline
    def add(self, item, **kwargs) -> str                # Simple storage
    def get(self, item_id: str) -> Optional[MemoryItem]
    def search(
        self,
        query: str,
        top_k: int = 5,
        memory_type: str = None,
        origin: str = None,              # prefix filter, e.g. "user"
        decompose_query: bool = False,   # split compound queries
        multi_hop: bool = False,         # chained recursive retrieval
        semantic_hops: bool = False,     # LLM-driven hop planning
        expertise: bool = False,         # return the expertise channel instead of a flat list
        include_superseded: bool = False,
        as_of_date=None,                 # bi-temporal recall (datetime | ISO-8601); see notes below
    ) -> List[MemoryItem]                # NOTE: with expertise=True, returns dict[str, list] keyed by
                                         # decision/constraint/learned/opinion/reasoning/observation
    def delete(self, item_id: str) -> bool

    # Expertise-layer capture (each returns a typed object, not a str)
    def add_decision(self, content: str, **kwargs) -> "Decision"    # .decision_id, .rejected_alternatives, .rationale, ...
    def add_constraint(self, content: str, **kwargs) -> "Constraint" # .constraint_id, ...
    def add_learning(self, content: str, **kwargs) -> "Learned"      # .learned_id, ...

    # Graph Integrity
    def delete_run(self, run_id: str) -> int
    def rename_entity_type(self, old: str, new: str) -> int
    def merge_entity_types(self, sources: List[str], target: str) -> int

    # Advanced
    def run_clustering(self) -> dict
    def run_evolution_cycle(self) -> None
    def personalize(self, traits: dict = None, preferences: dict = None) -> None
    def get_all_items_debug(self) -> Dict[str, Any]
    def close(self) -> None
```

### MemoryItem class

```python
@dataclass
class MemoryItem:
    content: str
    memory_type: str = 'semantic'
    item_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    valid_start_time: Optional[datetime] = None
    valid_end_time: Optional[datetime] = None
    transaction_time: datetime = field(default_factory=datetime.now)
    embedding: Optional[List[float]] = None
    entities: Optional[list] = None
    relations: Optional[list] = None
    metadata: dict = field(default_factory=dict)
```

</details>

## Configuration

Config file: `~/.config/smartmemory/config.toml` (XDG on Linux/macOS, `%APPDATA%\smartmemory\config.toml` on Windows).

API keys are stored in the OS keychain, never in the config file. Set `SMARTMEMORY_API_KEY` as an env var on headless systems where the keychain is unavailable.

### Non-interactive / CI

```bash
# Local
SMARTMEMORY_MODE=local smartmemory server

# Remote
SMARTMEMORY_MODE=remote SMARTMEMORY_API_KEY=sk_... smartmemory server
```

Env vars always override the config file, which is the correct path for Docker and CI. The setup TUI is automatically disabled in non-interactive environments.

<details>
<summary><strong>Environment variables</strong></summary>

| Variable | Description |
|----------|-------------|
| `SMARTMEMORY_MODE` | `local` or `remote`, overrides config file |
| `SMARTMEMORY_API_KEY` | API key for remote mode, bypasses keychain |
| `SMARTMEMORY_API_URL` | Remote API URL (default: `https://api.smartmemory.ai`) |
| `SMARTMEMORY_TEAM_ID` | Team/workspace ID for remote mode |
| `SMARTMEMORY_DATA_DIR` | Local data directory (default: `~/.smartmemory`) |
| `SMARTMEMORY_LLM_PROVIDER` | LLM provider for local enrichment |
| `SMARTMEMORY_EMBEDDING_PROVIDER` | Embedding provider (`local`, `openai`, `ollama`) |
| `SMARTMEMORY_DAEMON_PORT` | Daemon port (default: `9014`) |
| `SMARTMEMORY_ASYNC_ENRICHMENT` | Enable/disable background enrichment |
| `OPENAI_API_KEY` | OpenAI API key for embeddings and LLM extraction |
| `GROQ_API_KEY` | Groq API key, an alternative to OpenAI for LLM extraction |

</details>

## Testing

```bash
# Run all tests
PYTHONPATH=. pytest -v tests/

# Run specific test categories
PYTHONPATH=. pytest tests/unit/
PYTHONPATH=. pytest tests/integration/
PYTHONPATH=. pytest tests/e2e/
```

## License

SmartMemory is dual-licensed to provide flexibility for both open-source and commercial use. See [LICENSE](LICENSE) for details.

## Links

- **PyPI Package**: https://pypi.org/project/smartmemory/
- **Core Library**: https://pypi.org/project/smartmemory-core/
- **Documentation**: https://docs.smartmemory.ai
- **GitHub**: https://github.com/smart-memory
- **Issue Tracker**: https://github.com/smart-memory/smart-memory-core/issues

---

```bash
pip install smartmemory
smartmemory setup
```
