# Architecture Notes

Sake Concierge uses a BFF pattern so the browser never talks to Foundry directly.

```mermaid
sequenceDiagram
  participant U as User
  participant UI as React SPA
  participant API as FastAPI BFF
  participant A as Foundry Agent
  participant T as research_sake_brand
  participant C as Store catalog

  U->>UI: Ask for a sake recommendation
  UI->>API: POST /chat (SSE)
  API->>A: responses.create with AgentReference
  A-->>API: streamed text deltas
  A->>T: optional function call for external brand preference
  T-->>A: taste-axis research summary
  API->>C: resolve recommendation product IDs
  API-->>UI: delta + recommendations + done
```

## Key Design Choices

- Store data for recommendation is injected into Agent instructions at setup time by `StuffingRetriever`.
- The request-time `/chat` path forwards to an already-created Foundry Agent, avoiding per-request file search setup.
- CSV catalog facts are read separately by the BFF for cards, links, stock labels, and recommendation ID resolution.
- Feedback and analytics are split so normal KPI events do not persist chat text.
- The public sample keeps private catalog data and production prompt details out of Git.
