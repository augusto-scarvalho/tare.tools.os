# Direct-conversation attachment composition

Attachments need the Kernel's `attachments` extra, which the source install in the
README's "Getting started" already includes
(`"./satellites/tare.tools.kernel[attachments]"`). From that activated install, open
a project:

```powershell
tare "C:\path\to\your-project"
```

## Using the composer

Click **Attach**, choose **Attach a file** in Ctrl+P, or submit `/attach [PATH]`.
Browse with the arrow keys and Enter, or enter a folder/file path directly.
Relative paths start at the displayed folder; absolute paths may point outside
the project. **Up** opens the parent folder. Previewing and selecting a file do
not call a model.

Enter previews the selected file and saves a copy in external history storage.
Confirm to add it to the composer. Click its chip to reopen the saved preview;
click **×** to remove it. Changes or deletion of the original file do not change
the selected content. Images use a bounded terminal thumbnail; the model receives
the complete image bytes. Text previews show up to 12,000 characters and clearly
mark shortening; the complete accepted document is sent.

Select up to 16 UTF-8 documents or supported raster images. Attachments can be
sent alone, alongside text or alongside `@` file references. File references
point to current project files; attachments preserve the selected copy. Saved
drafts reopen their attachment chips. Switching workspaces clears the current
selection, and saved drafts remain bound to their original project.

You can prepare attachments while the assistant works. Sending queues the current
text and selected copies together; a failed save leaves the draft available to
retry. A saved or received message is not necessarily answered yet.

Images require an explicitly image-capable model profile. The current local-Qwen
profile does not declare vision support. An unsupported profile preserves the
draft and reports the required choice before showing the message as sent. The
console never switches to a paid model automatically. Existing sessions preserve
their saved profile; changing the next-conversation selection does not retarget
them.

## CLI input

Repeat `--attach FILE` to snapshot files on a new direct session. Omit the
instruction if the attachments themselves supply the input. For an existing
attachment-enabled session, use `--resume --follow-up "TEXT" --message-id ID`
with `--attach FILE`; `TEXT` may be empty. `--allow-attachments` enables a new
text-only session to receive attachments later.

The TUI hands saved copies to the CLI through `--attachment-input FILE`, a bounded
metadata file containing references and the external blob directory. It is
mutually exclusive with `--attach`. The operator-message CLI accepts the same
metadata option with `--text`, including empty text, but not with `--show`.

## Host API

The host first snapshots selected files with Kernel's
`NativeAttachmentStore(explicit_external_draft_directory)`. This lets the
composer preview show exactly the content that will be sent, independently
of later changes to the original file.

`native_agent.run_session` accepts:

- `accept_attachments=True` on a new session to enable later attachment input.
- `attachments=(ref, ...)` and `attachment_root=draft_store.root` for initial
  input or an explicit follow-up. Initial attachments also enable the feature.
- `attachment_files=(path, ...)` to import raw files directly; this cannot be
  combined with pre-existing attachment references.
- An empty instruction or follow-up when attachments supply the user input.
  Follow-ups still require `resume=True` and a stable `message_id`.

The session copies and verifies those bytes in its own external
`attachments/<sha256>` directory before recording the references. Omitting
`attachment_root` means the referenced bytes must already exist there. On resume,
initial input comes from the saved session. Completed turns are not rerun.
Selected projects cannot contain the session's custody directory.

`native_messages.enqueue` takes the same `attachments` and `attachment_root`
arguments for a session enabled for both messages and attachments. It copies
content before saving the queue entry. A retry compares text and every attachment
reference; the same ID cannot acknowledge different content. Inspection exposes
reference metadata and distinguishes `QUEUED` from `IN_CONTEXT`. These statuses
do not mean the assistant has answered the message.

Source-refresh notifications retain follow-up attachments. A refresh becomes
accepted only when the matching text and attachment references are in the Runtime
journal. Message delivery continues at Runtime's complete-round boundary.

## Deployment and compatibility

Attachment-enabled sessions inject a loader into Kernel and explicitly allow
HTTP requests up to 32 MiB. Larger response limits are not implied. For image
input, the saved deployment must explicitly include a supported list such as
`"native_image_media_types": ["image/png", "image/jpeg"]`. This declares deployment
capabilities; it does not prove that a model supports vision.

The bundled `bedrock-opus` and `bedrock-sonnet` profiles declare PNG, JPEG, static
GIF and WebP image input. AWS documents image input for
[Opus 4.6](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-4-6.html)
and [Sonnet 4.6](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html),
and the [Converse ImageBlock](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ImageBlock.html)
lists those formats. These declarations are documentation-backed; our local
transport checks use fixture providers and do not qualify live vision behavior.

Kernel refuses unconfigured image types before provider dispatch. Bedrock's
counter receives image content; existing local text/fleet counters are not
qualified for image tokens. Explicit estimates retain their unmeasured status
and require the existing context-policy opt-in. No counter or model fallback is
selected automatically. A load/validation error during counting blocks the turn
as `CONTEXT_COUNT_ERROR` without starting generation.

Sessions created without attachment support retain their old behavior and cannot
silently acquire the new capability on resume. Their normal text path remains
compatible with the installed Runtime. Attachment-enabled sessions require the
paired Runtime and Kernel; image validation additionally requires Pillow.

## Limits and remaining work

Content is limited to 16 MiB per file and 16 MiB of raw attachment bytes per
request, with additional provider image limits. Animated images, PDF and Office
extraction are not implemented; SVG is treated as text. Large-paste handling,
clipboard image input and real-model vision qualification remain open. Cancelled
previews can leave unreferenced copies in external storage; retention and cleanup
controls are still pending. See the full experience plan.
