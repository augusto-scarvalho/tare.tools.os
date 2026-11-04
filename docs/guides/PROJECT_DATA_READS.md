# Inspecting project JSON in a conversation

New native conversations can inspect minified JSON through the existing `read`
tool. This works in ordinary projects without a Backlog binding, structural index
or terminal permission. Paths remain relative to an explicitly selected project.

For example, an agent can inspect a graph with:

```json
{"repository":"project","path":"work-graph.json","json_pointer":"/nodes","json_fields":"/id,/title,/completion/status","offset":0,"limit":100}
```

The repository identifier comes from the session's tool schema. The response
contains the original file's SHA-256, a bounded JSON projection, item counts and
`next_offset`. Follow that offset to inspect the next page, supplying the returned
`sha256` to detect a changed source. An empty `json_pointer` inspects the root;
objects expose a directory of immediate keys, and deeper pointers select values.

These are structured views, not physical file lines. Their formatting must not
be used as exact replacement text. Source-line reads remain available for edits.
The existing 2 MiB source limit and 24 KB text budget remain in place.

## Roadmap and Backlog questions

Read the current roadmap's delivery boundary and deferred-work document alongside
the graph. Report recorded `completion.status` separately from admission labels,
documented implementation and acceptance evidence. Disagreements are findings to
reconcile; an answer does not update the graph or settle Work.

The optional `/backlog` binding still requires an explicit graph selection. Its
snapshot now includes recorded completion counts, a bounded inventory of node IDs
and statuses, and suggested next work. The inventory reports omitted nodes when
it reaches the snapshot budget; partial inventories cannot establish subset counts. A
snapshot is dated evidence; inspect current sources for a fresh investigation.
Selecting another project does not bind that project's graph automatically.

After an investigation, ask the same conversation to save its findings to a file.
That uses its existing edit permission and confirmed-effect history.

## Existing conversations

Start a new conversation to use structured reads. Existing conversations retain
their original tool schema so that resuming does not invalidate their saved model
identity. Their confirmed edits and original graph binding remain intact.

The paired OS and Kernel sources are required. Qualification and publication
status are recorded in the delivery evidence, separately from this usage guide.
