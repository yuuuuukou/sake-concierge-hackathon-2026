import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../../src/App";

vi.mock("../../src/hooks/useChat", () => ({
  useChat: () => ({
    clearConversation: vi.fn(),
    conversationId: null,
    error: null,
    isStreaming: false,
    messages: [],
    sendFeedback: vi.fn(),
    sendMessage: vi.fn(),
    trackProductLinkClick: vi.fn(),
    streamLabel: "待機中"
  })
}));

vi.mock("../../src/services/chatApi", () => ({
  fetchMetrics: vi.fn().mockResolvedValue({
    store_id: "fukunotomo",
    chat_requests: 0,
    feedback: { total: 0, positive: 0, negative: 0, positive_ratio: null },
    quality_targets: {
      golden_set_pass_rate: "90%以上",
      warm_first_delta: "3秒以内",
      cold_first_delta: "8秒以内"
    },
    privacy_note:
      "評価送信時は、この回答と直前の相談内容を品質改善のため記録します。個人情報、連絡先、住所、健康状態などは入力しないでください。",
    updated_at: "2026-05-12T00:00:00Z"
  }),
  fetchStoreProfile: vi.fn().mockResolvedValue({
    store_id: "fukunotomo",
    slug: "fukunotomo",
    display_name: "サンプル店舗",
    service_name: "酒あわせAI",
    headline: "今日の一本を、お好みから一緒に探します",
    description: "正確な価格・在庫は公式オンラインストアで確認してください。",
    location_label: "サンプル地域",
    data_label: "サンプル取扱い酒データ",
    product_count: 29,
    data_updated_on: "",
    featured_product_ids: [],
    quick_prompts: {
      ja: ["甘口で飲みやすいものを3本教えて"],
      en: ["Recommend three bottles"],
      zh: ["请推荐三款"]
    },
    next_actions: {
      ja: ["もっと辛口で"],
      en: ["Make it drier"],
      zh: ["再偏辛口一点"]
    },
    language_options: [
      { code: "ja", label: "日本語", short_label: "JP" },
      { code: "en", label: "English", short_label: "EN" },
      { code: "zh", label: "中文", short_label: "ZH" }
    ],
    compliance_notes: [
      "このサイトは非公式ファンサイトです。",
      "AI回答は参考情報です。正確な価格・在庫は公式オンラインストアで確認してください。",
      "20歳未満の飲酒は法律で禁止されています。飲酒は20歳になってから。"
    ],
    products: []
  })
}));

describe("App", () => {
  beforeEach(() => {
    HTMLElement.prototype.scrollTo = vi.fn();
    window.history.pushState({}, "", "/s/fukunotomo");
  });

  it("初期表示では利用上の注意を表示する", () => {
    render(<App />);

    expect(screen.queryByLabelText("重要な注意")).toBeNull();
    expect(screen.getByLabelText("利用上の注意")).toBeVisible();
    expect(screen.getByText("このサイトは非公式ファンサイトです。")).toBeVisible();
    expect(
      screen.getByText("AI回答は参考情報です。正確な価格・在庫は公式オンラインストアで確認してください。")
    ).toBeVisible();
    expect(screen.getByText("20歳未満の飲酒は法律で禁止されています。飲酒は20歳になってから。")).toBeVisible();
    expect(
      screen.getByText(
        "評価送信時は、この回答と直前の相談内容を品質改善のため記録します。個人情報、連絡先、住所、健康状態などは入力しないでください。"
      )
    ).toBeVisible();
  });

  it("お客様向け画面では多言語切替と好評価率を表示しない", () => {
    render(<App />);

    expect(screen.queryByLabelText("表示言語")).toBeNull();
    expect(screen.queryByText("好評価")).toBeNull();
  });

  it("非公式ファンサイト表記と質問例の見出しを表示する", async () => {
    render(<App />);

    expect(await screen.findByText("酒あわせAI")).toBeVisible();
    expect(await screen.findByText("サンプル店舗 ver.")).toBeVisible();
    expect(await screen.findByText("※このサイトは非公式ファンサイトです")).toBeVisible();
    expect(screen.getByText("例えば、こんなご質問にお答えします！（タップで質問ができます）")).toBeVisible();
  });
});

