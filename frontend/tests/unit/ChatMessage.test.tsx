import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChatMessage, safeUrlTransform } from "../../src/components/ChatMessage";

describe("ChatMessage", () => {
  it("Markdown の商品リンクを別タブで開ける形で表示する", () => {
    render(
      <ChatMessage
        message={{
          id: "assistant-1",
          role: "assistant",
          status: "complete",
          content: "[商品ページ](https://shop.example/sake/1) を確認できます。"
        }}
      />
    );

    const link = screen.getByRole("link", { name: "商品ページ" });
    expect(link).toHaveAttribute("href", "https://shop.example/sake/1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("危険な URL scheme は許可しない", () => {
    expect(safeUrlTransform("javascript:alert(1)")).toBe("");
    expect(safeUrlTransform("https://shop.example/sake/1")).toBe("https://shop.example/sake/1");
  });

  it("推薦カード、次アクション、feedback を表示する", async () => {
    const onAction = vi.fn();
    const onFeedback = vi.fn();
    const onProductLinkClick = vi.fn();
    const product = {
      id: "ftm-fuyuki-fff-genshu",
      name: "純米吟醸原酒 冬樹FFF",
      brewery_name: "サンプル店舗",
      series: "サンプル分類",
      style_class: "純米吟醸原酒",
      taste_type: "香りのある旨口",
      taste_tags: ["旨み強い"],
      aroma_tags: ["フルーティー"],
      service_methods: ["冷酒"],
      pairing_tags: ["チーズ"],
      reason_tags: ["香りのある旨口", "冷酒"],
      summary: "豊かな旨みと甘みが際立つ候補です。",
      official_url: "https://example.com/products/sample-00639",
      price_label: "価格は公式確認",
      stock_status: "needs_verification",
      stock_label: "在庫は公式商品ページで確認ください",
      verified_on: "",
      aliases: ["冬樹FFF"],
      skus: []
    };
    render(
      <ChatMessage
        message={{
          id: "assistant-2",
          role: "assistant",
          status: "complete",
          content: "冬樹FFFがおすすめです。",
          actions: ["もっと辛口で"],
          recommendations: [product]
        }}
        onAction={onAction}
        onFeedback={onFeedback}
        onProductLinkClick={onProductLinkClick}
      />
    );

    expect(screen.getByRole("heading", { name: "純米吟醸原酒 冬樹FFF" })).toBeVisible();
    expect(screen.queryByText("サンプル分類")).toBeNull();
    expect(screen.getByRole("link", { name: "商品ページ" })).toHaveAttribute(
      "href",
      "https://example.com/products/sample-00639"
    );
    await userEvent.click(screen.getByRole("link", { name: "商品ページ" }));
    expect(onProductLinkClick).toHaveBeenCalledWith("assistant-2", product, 1);

    await userEvent.click(screen.getByRole("button", { name: "もっと辛口で" }));
    expect(onAction).toHaveBeenCalledWith("もっと辛口で");

    const submitFeedbackButton = screen.getByRole("button", { name: "コメントを送信" });
    expect(submitFeedbackButton).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "役に立った" }));
    expect(onFeedback).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "役に立った" })).toHaveAttribute("aria-pressed", "true");
    expect(submitFeedbackButton).toBeEnabled();

    await userEvent.type(screen.getByLabelText("フィードバックメモ"), "香りの説明が助かりました");
    await userEvent.click(submitFeedbackButton);
    expect(onFeedback).toHaveBeenCalledWith("assistant-2", "positive", "香りの説明が助かりました");
  });

  it("streaming 中は次アクションを押せない表示にする", () => {
    render(
      <ChatMessage
        actionsDisabled
        message={{
          id: "assistant-3",
          role: "assistant",
          status: "complete",
          content: "候補を出しました。",
          actions: ["もっと辛口で"]
        }}
      />
    );

    expect(screen.getByRole("button", { name: "もっと辛口で" })).toBeDisabled();
  });

  it("送信済み feedback のコメントを再送できる", async () => {
    const onFeedback = vi.fn();
    const { rerender } = render(
      <ChatMessage
        message={{
          id: "assistant-4",
          role: "assistant",
          status: "complete",
          content: "冬樹FFFがおすすめです。",
          feedback: { rating: "positive", status: "sent" }
        }}
        onFeedback={onFeedback}
      />
    );

    expect(screen.getByRole("button", { name: "役に立った" })).toHaveAttribute("aria-pressed", "true");
    const feedbackMemo = screen.getByLabelText("フィードバックメモ");
    await userEvent.type(feedbackMemo, "コメントを追記します");
    await userEvent.click(screen.getByRole("button", { name: "コメントを更新" }));
    expect(onFeedback).toHaveBeenCalledWith("assistant-4", "positive", "コメントを追記します");
    expect(screen.getByText("フィードバックを記録しました。")).toBeVisible();
    expect(screen.queryByText("コメントを直したら再送信できます。")).toBeNull();

    await userEvent.type(feedbackMemo, "再送後に消えます");
    rerender(
      <ChatMessage
        message={{
          id: "assistant-4",
          role: "assistant",
          status: "complete",
          content: "冬樹FFFがおすすめです。",
          feedback: { rating: "positive", status: "sending" }
        }}
        onFeedback={onFeedback}
      />
    );
    rerender(
      <ChatMessage
        message={{
          id: "assistant-4",
          role: "assistant",
          status: "complete",
          content: "冬樹FFFがおすすめです。",
          feedback: { rating: "positive", status: "sent" }
        }}
        onFeedback={onFeedback}
      />
    );
    expect(screen.getByLabelText("フィードバックメモ")).toHaveValue("");
  });
});


