import type {
  AnalyticsEventRequest,
  ChatRequest,
  ConversationResponse,
  FeedbackRequest,
  MetricsSummary,
  SseEvent,
  SseEventName,
  StoreProfile,
  StreamHandlers
} from "../types/chat";

export async function fetchStoreProfile(storeId: string, signal?: AbortSignal): Promise<StoreProfile> {
  const response = await fetch(`/api/stores/${encodeURIComponent(storeId)}`, {
    method: "GET",
    signal
  });

  if (!response.ok) {
    throw new Error(await readErrorResponse(response));
  }

  return (await response.json()) as StoreProfile;
}

export async function fetchMetrics(storeId: string, signal?: AbortSignal): Promise<MetricsSummary> {
  const response = await fetch(`/api/stores/${encodeURIComponent(storeId)}/metrics`, {
    method: "GET",
    signal
  });

  if (!response.ok) {
    throw new Error(await readErrorResponse(response));
  }

  return (await response.json()) as MetricsSummary;
}

export async function createConversation(signal?: AbortSignal): Promise<string> {
  const response = await fetch("/chat/conversation", {
    method: "POST",
    signal
  });

  if (!response.ok) {
    throw new Error(await readErrorResponse(response));
  }

  const body = (await response.json()) as ConversationResponse;
  if (!body.conversation_id) {
    throw new Error("会話IDを取得できませんでした。");
  }

  return body.conversation_id;
}

export async function streamChat(
  request: ChatRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal
) {
  const response = await fetch("/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json; charset=utf-8"
    },
    body: JSON.stringify(request),
    signal
  });

  if (!response.ok) {
    throw new Error(await readErrorResponse(response));
  }

  if (!response.body) {
    throw new Error("サーバーからストリームを取得できませんでした。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawTerminalEvent = false;
  const terminalAwareHandlers: StreamHandlers = {
    ...handlers,
    onDone: () => {
      sawTerminalEvent = true;
      handlers.onDone();
    },
    onError: (message) => {
      sawTerminalEvent = true;
      handlers.onError(message);
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    buffer = dispatchBufferedEvents(buffer, terminalAwareHandlers);
  }

  buffer += decoder.decode().replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  dispatchBufferedEvents(`${buffer}\n\n`, terminalAwareHandlers);
  if (!sawTerminalEvent) {
    throw new Error("サーバーから応答完了イベントを受信できませんでした。もう一度送信してください。");
  }
}

export async function submitFeedback(
  request: FeedbackRequest,
  signal?: AbortSignal
): Promise<MetricsSummary> {
  const response = await fetch("/api/feedback", {
    method: "POST",
    headers: {
      "Content-Type": "application/json; charset=utf-8"
    },
    body: JSON.stringify(request),
    signal
  });

  if (!response.ok) {
    throw new Error(await readErrorResponse(response));
  }

  const body = (await response.json()) as { metrics: MetricsSummary };
  return body.metrics;
}

export async function submitAnalyticsEvent(
  request: AnalyticsEventRequest,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetch("/api/analytics/events", {
    method: "POST",
    headers: {
      "Content-Type": "application/json; charset=utf-8"
    },
    body: JSON.stringify(request),
    keepalive: true,
    signal
  });

  if (!response.ok) {
    throw new Error(await readErrorResponse(response));
  }
}

async function readErrorResponse(response: Response): Promise<string> {
  const contentType = response.headers.get("content-type") ?? "";
  const text = await response.text();
  if (!text) {
    return `HTTP ${response.status}: ${response.statusText}`;
  }

  if (looksLikeHtmlResponse(text, contentType)) {
    return (
      "チャットAPIに接続できませんでした。FastAPI の起動ポートと " +
      "VITE_API_PROXY_TARGET が一致しているか確認してください。"
    );
  }

  try {
    const body = JSON.parse(text) as { detail?: unknown };
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // plain text fallback
  }

  return text;
}

function looksLikeHtmlResponse(text: string, contentType: string): boolean {
  const trimmedText = text.trimStart().toLowerCase();
  return contentType.includes("text/html") || trimmedText.startsWith("<!doctype html") || trimmedText.startsWith("<html");
}

function dispatchBufferedEvents(buffer: string, handlers: StreamHandlers): string {
  let remaining = buffer;
  let separatorIndex = remaining.indexOf("\n\n");

  while (separatorIndex >= 0) {
    const rawEvent = remaining.slice(0, separatorIndex);
    remaining = remaining.slice(separatorIndex + 2);
    const event = parseSseEvent(rawEvent);
    dispatchEvent(event, handlers);
    separatorIndex = remaining.indexOf("\n\n");
  }

  return remaining;
}

function parseSseEvent(rawEvent: string): SseEvent {
  let eventName: SseEventName = "message";
  const dataLines: string[] = [];

  for (const line of rawEvent.split("\n")) {
    if (!line || line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trim() as SseEventName;
      continue;
    }
    if (line.startsWith("data:")) {
      const value = line.slice("data:".length);
      dataLines.push(value.startsWith(" ") ? value.slice(1) : value);
    }
  }

  return {
    event: eventName,
    data: dataLines.join("\n")
  };
}

function dispatchEvent(event: SseEvent, handlers: StreamHandlers) {
  switch (event.event) {
    case "meta":
      handleMetaEvent(event.data, handlers);
      return;
    case "delta":
      handlers.onDelta(event.data);
      return;
    case "status":
      handlers.onStatus(event.data);
      return;
    case "recommendations":
      handleRecommendationsEvent(event.data, handlers);
      return;
    case "done":
      handlers.onDone();
      return;
    case "error":
      handlers.onError(event.data || "応答の取得中にエラーが発生しました。");
      return;
    case "message":
      return;
  }
}

function handleRecommendationsEvent(data: string, handlers: StreamHandlers) {
  try {
    const payload = JSON.parse(data) as { product_ids?: unknown };
    if (Array.isArray(payload.product_ids)) {
      handlers.onRecommendations?.(
        payload.product_ids.filter((productId): productId is string => typeof productId === "string")
      );
    }
  } catch {
    handlers.onError("推薦カード情報の読み取りに失敗しました。");
  }
}

function handleMetaEvent(data: string, handlers: StreamHandlers) {
  try {
    const payload = JSON.parse(data) as { conversation_id?: unknown };
    if (typeof payload.conversation_id === "string") {
      handlers.onMeta(payload.conversation_id);
    }
  } catch {
    handlers.onError("会話IDの読み取りに失敗しました。");
  }
}
