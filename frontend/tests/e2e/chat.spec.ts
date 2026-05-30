import { expect, test } from "@playwright/test";

test("SSE 応答を表示し、conversation_id を次の相談へ引き継ぐ", async ({ page }) => {
  const requests: Array<{
    message: string;
    conversation_id?: string | null;
    session_id?: string | null;
    store_id?: string;
    language?: string;
  }> = [];
  const feedbackRequests: Array<{
    store_id?: string;
    session_id?: string | null;
    conversation_id?: string | null;
    message_id?: string;
    rating?: string;
    comment?: string;
    user_message?: string;
    assistant_message?: string;
    language?: string;
  }> = [];
  const analyticsRequests: Array<{
    event_type?: string;
    store_id?: string;
    session_id?: string | null;
    conversation_id?: string | null;
    message_id?: string;
    product_id?: string;
    product_ids?: string[];
    recommendation_rank?: number;
    official_url?: string;
    page_path?: string;
    language?: string;
  }> = [];

  await routeStoreApis(page);

  await page.route("**/chat/conversation", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8" },
      body: JSON.stringify({ conversation_id: "conv_e2e" })
    });
  });

  await page.route("**/chat", async (route) => {
    const body = route.request().postDataJSON() as { message: string; conversation_id?: string | null };
    requests.push(body);

    const responseBody =
      requests.length === 1
        ? [
            'event: meta\ndata: {"conversation_id":"conv_e2e"}',
            "event: delta\ndata: 純米吟醸生原酒 冬樹FFF がおすすめです。",
            "event: delta\ndata: [商品ページ](https://shop.example/sake/1)",
            'event: recommendations\ndata: {"product_ids":["fuyuki-fff"]}',
            "event: done\ndata: "
          ].join("\n\n")
        : [
            'event: meta\ndata: {"conversation_id":"conv_e2e"}',
            "event: delta\ndata: では刈穂の辛口も候補に入ります。",
            "event: done\ndata: "
          ].join("\n\n");

    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8" },
      body: `${responseBody}\n\n`
    });
  });
  await page.route("**/api/feedback", async (route) => {
    feedbackRequests.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        status: "ok",
        metrics: {
          store_id: "fukunotomo",
          chat_requests: 1,
          feedback: { total: 1, positive: 1, negative: 0, positive_ratio: 1 },
          quality_targets: {
            golden_set_pass_rate: "90%以上",
            warm_first_delta: "3秒以内",
            cold_first_delta: "8秒以内"
          },
          privacy_note:
            "評価送信時は、この回答と直前の相談内容を品質改善のため記録します。個人情報、連絡先、住所、健康状態などは入力しないでください。",
          updated_at: "2026-05-12T00:00:00Z"
        }
      })
    });
  });
  await page.route("**/api/analytics/events", async (route) => {
    analyticsRequests.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8" },
      body: JSON.stringify({ status: "ok" })
    });
  });

  await page.goto("/s/fukunotomo");
  await expect(page.getByText("酒あわせAI", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("サンプル店舗 ver.")).toBeVisible();
  await expect(page.locator(".unofficial-badge")).toHaveText("※このサイトは非公式ファンサイトです");
  await expect(page.getByText("サンプル地域")).toHaveCount(0);
  await expect(page.getByText("例えば、こんなご質問にお答えします！（タップで質問ができます）")).toBeVisible();
  await expect(page.getByLabel("相談内容")).toBeVisible();
  await expect(page.getByRole("button", { name: "チャット履歴をクリア" })).toBeDisabled();
  await expect(page.locator(".header-clear-button")).toContainText("チャット履歴をクリア");
  await expect(page.locator(".header-clear-button br")).toHaveCount(1);
  await expect(page.getByText("好評価")).toHaveCount(0);
  await expect(page.locator('[aria-label="重要な注意"]')).toHaveCount(0);
  await expect(page.locator('[aria-label="利用上の注意"]')).toBeVisible();
  await expect(
    page.getByText(
      "評価送信時は、この回答と直前の相談内容を品質改善のため記録します。個人情報、連絡先、住所、健康状態などは入力しないでください。"
    )
  ).toBeVisible();
  await expect(page.getByText("相談中 #conv_e2e")).toBeVisible();

  await page.getByRole("button", { name: "甘口で飲みやすいものを3本教えて" }).click();

  await expect(page.getByRole("button", { name: "店舗情報・注意事項" })).toBeVisible();
  await expect(page.locator('[aria-label="重要な注意"]')).toHaveCount(0);
  await expect(page.getByText("純米吟醸生原酒 冬樹FFF がおすすめです。")).toBeVisible();
  await expect(page.getByRole("link", { name: "商品ページ" }).first()).toHaveAttribute(
    "href",
    "https://shop.example/sake/1"
  );
  await expect(page.getByLabel("推薦カード").getByText("純米吟醸生原酒 冬樹FFF")).toBeVisible();
  await expect(page.getByRole("link", { name: "商品ページ" }).last()).toHaveAttribute(
    "href",
    "https://shop.example/sake/fuyuki"
  );
  await expect(page.getByLabel("推薦カード").getByText("馬から 辛口酒")).toHaveCount(0);
  await expect(page.getByText("相談中 #conv_e2e")).toBeVisible();
  await expect.poll(
    () => analyticsRequests.filter((request) => request.event_type === "recommendation_shown").length
  ).toBe(1);

  await page.reload();
  await expect(page.getByText("純米吟醸生原酒 冬樹FFF がおすすめです。")).toBeVisible();
  await expect(page.getByLabel("推薦カード").getByText("純米吟醸生原酒 冬樹FFF")).toBeVisible();
  await expect(page.getByText("相談中 #conv_e2e")).toBeVisible();
  await expect(page.getByRole("button", { name: "チャット履歴をクリア" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "店舗情報・注意事項" })).toBeVisible();

  const restoredPosition = await page.locator(".chat-scroll-region").evaluate((container) => {
    const userMessages = container.querySelectorAll('[data-message-role="user"]');
    const lastUserMessage = userMessages[userMessages.length - 1];
    if (!(lastUserMessage instanceof HTMLElement)) {
      return null;
    }
    const containerRect = container.getBoundingClientRect();
    const messageRect = lastUserMessage.getBoundingClientRect();
    return {
      offsetTop: messageRect.top - containerRect.top,
      scrollTop: container.scrollTop
    };
  });
  expect(restoredPosition).not.toBeNull();
  expect(restoredPosition?.scrollTop).toBeGreaterThan(0);
  expect(restoredPosition?.offsetTop).toBeGreaterThanOrEqual(0);
  expect(restoredPosition?.offsetTop).toBeLessThan(128);

  const productPage = page.context().waitForEvent("page");
  await page.getByLabel("推薦カード").getByRole("link", { name: "商品ページ" }).click();
  await (await productPage).close();
  await expect.poll(
    () => analyticsRequests.filter((request) => request.event_type === "product_link_clicked").length
  ).toBe(1);

  await page.getByRole("button", { name: "役に立った" }).click();
  expect(feedbackRequests).toHaveLength(0);
  await page.getByLabel("フィードバックメモ").fill("香りの説明が助かりました");
  await page.getByRole("button", { name: "コメントを送信" }).click();
  await expect(page.getByText("フィードバックを記録しました。")).toBeVisible();
  await page.getByLabel("フィードバックメモ").fill("香りと温度の説明が助かりました");
  await page.getByRole("button", { name: "コメントを更新" }).click();
  await expect.poll(() => feedbackRequests.length).toBe(2);

  await page.getByRole("button", { name: "店舗情報・注意事項" }).click();
  await expect(page.locator('[aria-label="利用上の注意"]')).toBeVisible();

  await page.getByLabel("相談内容").fill("もう少し辛口だと？");
  await page.getByRole("button", { name: "送信" }).click();

  await expect(page.getByText("では刈穂の辛口も候補に入ります。")).toBeVisible();
  await page.getByRole("button", { name: "チャット履歴をクリア" }).click();
  await expect(page.getByText("純米吟醸生原酒 冬樹FFF がおすすめです。")).toHaveCount(0);
  await expect(page.getByText("では刈穂の辛口も候補に入ります。")).toHaveCount(0);
  await expect(page.getByText("新しい相談")).toBeVisible();
  await page.reload();
  await expect(page.getByText("純米吟醸生原酒 冬樹FFF がおすすめです。")).toHaveCount(0);
  await expect(page.getByText("では刈穂の辛口も候補に入ります。")).toHaveCount(0);
  expect(requests).toEqual([
    {
      message: "甘口で飲みやすいものを3本教えて",
      conversation_id: "conv_e2e",
      session_id: expect.stringMatching(/^session-/),
      store_id: "fukunotomo",
      language: "ja"
    },
    {
      message: "もう少し辛口だと？",
      conversation_id: "conv_e2e",
      session_id: expect.stringMatching(/^session-/),
      store_id: "fukunotomo",
      language: "ja"
    }
  ]);
  expect(feedbackRequests).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        store_id: "fukunotomo",
        session_id: expect.stringMatching(/^session-/),
        conversation_id: "conv_e2e",
        rating: "positive",
        comment: "香りの説明が助かりました",
        user_message: "甘口で飲みやすいものを3本教えて",
        assistant_message: expect.stringContaining("純米吟醸生原酒 冬樹FFF がおすすめです。"),
        language: "ja"
      }),
      expect.objectContaining({
        store_id: "fukunotomo",
        session_id: expect.stringMatching(/^session-/),
        conversation_id: "conv_e2e",
        rating: "positive",
        comment: "香りと温度の説明が助かりました",
        user_message: "甘口で飲みやすいものを3本教えて",
        assistant_message: expect.stringContaining("純米吟醸生原酒 冬樹FFF がおすすめです。"),
        language: "ja"
      })
    ])
  );
  expect(analyticsRequests).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        event_type: "recommendation_shown",
        store_id: "fukunotomo",
        session_id: expect.stringMatching(/^session-/),
        conversation_id: "conv_e2e",
        product_ids: ["fuyuki-fff"],
        page_path: "/s/fukunotomo",
        language: "ja"
      }),
      expect.objectContaining({
        event_type: "product_link_clicked",
        store_id: "fukunotomo",
        session_id: expect.stringMatching(/^session-/),
        conversation_id: "conv_e2e",
        product_id: "fuyuki-fff",
        recommendation_rank: 1,
        official_url: "https://shop.example/sake/fuyuki",
        page_path: "/s/fukunotomo",
        language: "ja"
      })
    ])
  );
});

test("プロキシ先が違うときの HTML エラーを利用者向けに表示する", async ({ page }) => {
  await routeStoreApis(page);

  await page.route("**/chat/conversation", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8" },
      body: JSON.stringify({ conversation_id: "conv_e2e" })
    });
  });

  await page.route("**/chat", async (route) => {
    await route.fulfill({
      status: 404,
      headers: { "content-type": "text/html; charset=utf-8" },
      body: "<!DOCTYPE html><html><body><pre>Cannot POST /chat</pre></body></html>"
    });
  });

  await page.goto("/");
  await page.getByLabel("相談内容").fill("こんにちは");
  await page.getByRole("button", { name: "送信" }).click();

  await expect(page.getByText("チャットAPIに接続できませんでした。", { exact: false })).toBeVisible();
  await expect(page.getByText("Cannot POST /chat")).toHaveCount(0);
});

async function routeStoreApis(page: import("@playwright/test").Page) {
  await page.route("**/api/stores/fukunotomo", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        store_id: "fukunotomo",
        slug: "fukunotomo",
        display_name: "サンプル店舗",
        service_name: "酒あわせAI",
        headline: "今日の一本を、お好みから一緒に探します",
        description: "正確な価格・在庫は公式オンラインストアで確認してください。",
        location_label: "サンプル地域",
        data_label: "サンプル取扱い酒データ",
        product_count: 3,
        featured_product_ids: ["umakarakuchi"],
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
        products: [
          {
            id: "fuyuki-fff",
            name: "純米吟醸生原酒 冬樹FFF",
            brewery_name: "サンプル店舗",
            series: "季節限定",
            style_class: "純米吟醸",
            taste_type: "甘口",
            taste_tags: ["甘口", "華やか"],
            aroma_tags: ["サンプル香"],
            service_methods: ["冷酒"],
            pairing_tags: ["魚料理"],
            reason_tags: ["甘口", "冷や良し"],
            summary: "華やかな甘みと酸味で、冷やして飲みやすい一本。",
            official_url: "https://shop.example/sake/fuyuki",
            price_label: "価格は公式確認",
            stock_status: "needs_verification",
            stock_label: "在庫は公式商品ページで確認ください",
            verified_on: "",
            aliases: ["純米吟醸生原酒 冬樹FFF", "冬樹FFF"],
            skus: []
          },
          {
            id: "umakarakuchi",
            name: "馬から 辛口酒",
            brewery_name: "サンプル店舗",
            series: "日常食中酒",
            style_class: "辛口酒",
            taste_type: "辛口",
            taste_tags: ["辛口"],
            aroma_tags: ["穏やか"],
            service_methods: ["冷酒", "燗"],
            pairing_tags: ["和食"],
            reason_tags: ["辛口"],
            summary: "食事に合わせやすい辛口。",
            official_url: "https://shop.example/sake/umakara",
            price_label: "価格は公式確認",
            stock_status: "needs_verification",
            stock_label: "在庫は公式商品ページで確認ください",
            verified_on: "",
            aliases: ["馬から 辛口酒"],
            skus: []
          }
        ]
      })
    });
  });

  await page.route("**/api/stores/fukunotomo/metrics", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8" },
      body: JSON.stringify({
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
      })
    });
  });
}


