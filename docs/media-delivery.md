# Media delivery

Talos delivers requested files to the current conversation. A final response uses a
standalone `MEDIA:<local path>` line for each attachment; the control line is hidden
from Telegram progress and the final visible message. A remote filename is not a
local attachment: retrieve the verified file with the installation's authorized tools
first. Do not regenerate an existing result just to send it.

Telegram presentation is selected from file bytes, not extensions:

| File | Presentation |
| --- | --- |
| PNG, JPEG up to 10 MB | Photo |
| GIF | Animation |
| MP3, M4A container | Audio player |
| Ogg Opus | Voice message |
| Recognized MP4 video container | Video player |
| PDF, office files, archives, WebP, unknown formats | Original document |

Telegram's supported player formats are described in its
[Bot API](https://core.telegram.org/bots/api#available-methods). A recognized container
does not prove that a damaged file or every codec will play. Unsupported files can be
sent as documents. Talos currently accepts at most four attachments per answer and
20 MB per attachment. Larger files are refused explicitly and retained.

## Confirmed delivery and disposable copies

The Telegram client requires a successful API result, the expected conversation,
a positive message ID and the matching media record. HTTP success alone is not enough.
The event log records the message ID with `attachment.sent`. An uncertain response
does not trigger automatic retry: check the chat before resending to avoid duplicates.
Conversation memory also receives these delivery facts, including local cleanup,
so a follow-up does not have to infer delivery from the model's earlier wording.

The conductor supplies the actual user channel and its file capability to the model.
For native Hermes-backed providers, Talos uses quiet single-query chat with a temporary
transport context. The CLI is only the model transport, not the destination of the
reply. The disabled native-tool preflight remains required; Hermes configuration is
not changed. Kimi's separate adapter keeps its one-shot command protocol.

Automatic cleanup is **off by default**. An operator can set
`TALOS_CLEANUP_SENT_MEDIA=1` in their protected service configuration. This removes only
unchanged disposable files directly inside `workspace/outbox/`, after a confirmed
Telegram upload and after recording the delivery receipt. Use unique names; keep
originals, reference inputs and project files outside the outbox. Symlinks, hardlinks
and nested directories are not cleanup candidates. Existing attachment roots, secret
filters, recipient selection and tool approvals still apply.

Failed or unconfirmed uploads retain their files. Changed files and cleanup failures
retain the local copy and report that delivery already succeeded; they do not resend
the attachment. `attachment.cleanup` links the cleanup result to the Telegram message
ID. API confirmation proves that Telegram accepted the message, not that a person
opened it. Channels without a structured upload receipt do not trigger cleanup.
