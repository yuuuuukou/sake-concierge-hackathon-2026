import { describe, expect, it, vi } from "vitest";
import {
  createConversation,
  fetchMetrics,
  fetchStoreProfile,
  streamChat,
  submitAnalyticsEvent,
  submitFeedback
} from "../../src/services/chatApi";
import type { StreamHandlers } from "../../src/types/chat";

describe("createConversation", () => {
  it("メッセージ送信前に conversation_id を取得する", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ conversation_id: "conv_prefetched" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(createConversation()).resolves.toBe("conv_prefetched");
    expect(fetchMock).toHaveBeenCalledWith(
      "/chat/conversation",
      expect.objectContaining({
        method: "POST"
      })
    );
  });
});

describe("streamChat", () => {
  it("SSE の meta / delta / done を順に処理する", async () => {
    const chunks = [
      'event: meta\ndata: {"conversation_id":"conv_123"}\n\n',
      "event: delta\ndata: 地域の\n\n",
      "event: delta\ndata: 辛口です\n\n",
      'event: recommendations\ndata: {"product_ids":["ftm-jinguuji"]}\n\n',
      "event: done\ndata: \n\n"
    ];
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(createStream(chunks), {
        status: 200,
        headers: { "content-type": "text/event-stream" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const events: string[] = [];
    const handlers: StreamHandlers = {
      onMeta: (conversationId) => events.push(`meta:${conversationId}`),
      onDelta: (delta) => events.push(`delta:${delta}`),
      onRecommendations: (productIds) => events.push(`recommendations:${productIds.join(",")}`),
      onStatus: (status) => events.push(`status:${status}`),
      onDone: () => events.push("done"),
      onError: (message) => events.push(`error:${message}`)
    };

    await streamChat({ message: "辛口を教えて", conversation_id: "conv_old" }, handlers);

    expect(fetchMock).toHaveBeenCalledWith(
      "/chat",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ message: "辛口を教えて", conversation_id: "conv_old" })
      })
    );
    expect(events).toEqual([
      "meta:conv_123",
      "delta:地域の",
      "delta:辛口です",
      "recommendations:ftm-jinguuji",
      "done"
    ]);
  });

  it("HTML エラーをそのまま UI に出さず、接続先の確認メッセージに変換する", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!DOCTYPE html><pre>Cannot POST /chat</pre>", {
          status: 404,
          statusText: "Not Found",
          headers: { "content-type": "text/html; charset=utf-8" }
        })
      )
    );

    await expect(
      streamChat({ message: "こんにちは" }, createNoopHandlers())
    ).rejects.toThrow("VITE_API_PROXY_TARGET");
  });

  it("done/error なしでストリームが切れたら正常扱いにしない", async () => {
    const chunks = [
      'event: meta\ndata: {"conversation_id":"conv_123"}\n\n',
      "event: delta\ndata: 途中までの回答\n\n"
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(createStream(chunks), {
          status: 200,
          headers: { "content-type": "text/event-stream" }
        })
      )
    );

    await expect(
      streamChat({ message: "辛口を教えて" }, createNoopHandlers())
    ).rejects.toThrow("応答完了イベント");
  });
});

describe("store APIs", () => {
  it("店舗プロファイルを取得する", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ store_id: "fukunotomo", products: [] }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchStoreProfile("fukunotomo")).resolves.toMatchObject({
      store_id: "fukunotomo"
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/stores/fukunotomo",
      expect.objectContaining({ method: "GET" })
    );
  });

  it("店舗メトリクスを取得する", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ store_id: "fukunotomo", chat_requests: 1 }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchMetrics("fukunotomo")).resolves.toMatchObject({
      chat_requests: 1
    });
  });

  it("feedback を送信して metrics を返す", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          metrics: {
            store_id: "fukunotomo",
            chat_requests: 0,
            feedback: { total: 1, positive: 1, negative: 0, positive_ratio: 1 }
          }
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" }
        }
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      submitFeedback({
        store_id: "fukunotomo",
        message_id: "assistant-1",
        rating: "positive",
        comment: "よい",
        language: "ja"
      })
    ).resolves.toMatchObject({
      feedback: { total: 1, positive_ratio: 1 }
    });
  });

  it("analytics event を送信する", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      submitAnalyticsEvent({
        event_type: "product_link_clicked",
        store_id: "fukunotomo",
        session_id: "session-1",
        conversation_id: "conv-1",
        message_id: "assistant-1",
        product_id: "ftm-fuyuki-fff-genshu",
        recommendation_rank: 1,
        official_url: "https://example.com/products/sample-00639",
        page_path: "/s/fukunotomo",
        language: "ja"
      })
    ).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/analytics/events",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          event_type: "product_link_clicked",
          store_id: "fukunotomo",
          session_id: "session-1",
          conversation_id: "conv-1",
          message_id: "assistant-1",
          product_id: "ftm-fuyuki-fff-genshu",
          recommendation_rank: 1,
          official_url: "https://example.com/products/sample-00639",
          page_path: "/s/fukunotomo",
          language: "ja"
        })
      })
    );
  });
});

function createStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();

  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    }
  });
}

function createNoopHandlers(): StreamHandlers {
  return {
    onMeta: () => undefined,
    onDelta: () => undefined,
    onStatus: () => undefined,
    onDone: () => undefined,
    onError: () => undefined
  };
}

