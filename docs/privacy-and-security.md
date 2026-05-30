# Privacy And Security Notes

This public snapshot demonstrates the production guardrails without exposing production configuration.

## Data Handling

- Normal chat traces store structured metadata such as status, elapsed time, and recommended product IDs.
- User and assistant text is suppressed by default when `CHAT_TEXT_CAPTURE_MODE=feedback_only`.
- Feedback submissions can include the latest user/assistant text, but the backend applies simple masking for emails, phone numbers, postal codes, and URLs before logging.
- Browser `session_id` values are hashed before logs are written. Set `SESSION_HASH_SALT` to a random per-environment value outside Git.

## Runtime Guards

- `/chat`, `/chat/conversation`, feedback, analytics, and A2A endpoints have lightweight in-memory rate limits.
- A2A JSON-RPC is hidden unless `A2A_API_KEY` is configured.
- Security headers are applied to API responses.
- Production store data should be loaded from private Blob Storage with Managed Identity, not committed to Git or baked into images.

## Public Repository Scope

The included CSV/Markdown files are sample fixtures for local demos and tests. They are not the production catalog or production prompt pack.
