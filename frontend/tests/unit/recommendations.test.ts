import { describe, expect, it } from "vitest";
import { selectRecommendationCards } from "../../src/services/recommendations";
import type { StoreProduct, StoreProfile } from "../../src/types/chat";

describe("selectRecommendationCards", () => {
  it("本文に11銘柄ある場合はデフォルトで10枚までカード化する", () => {
    const products = Array.from({ length: 11 }, (_, index) =>
      createProduct(`product-${index + 1}`, `テスト酒${index + 1}`)
    );
    const profile = createProfile(products);

    const cards = selectRecommendationCards(
      products.map((product, index) => `${index + 1}. ${product.name}`).join("\n"),
      profile
    );

    expect(cards.map((product) => product.id)).toEqual(
      products.slice(0, 10).map((product) => product.id)
    );
  });

  it("短い銘柄名が長い銘柄名の途中に出た場合は次の出現位置も確認する", () => {
    const fffNama = createProduct("fuyuki-fff-nama", "純米吟醸生原酒 冬樹FFF");
    const fuyukiNama = createProduct("fuyuki-nama", "純米吟醸生原酒 冬樹");
    const profile = createProfile([fffNama, fuyukiNama]);

    expect(
      selectRecommendationCards("1. 純米吟醸生原酒 冬樹FFF", profile).map(
        (product) => product.id
      )
    ).toEqual(["fuyuki-fff-nama"]);

    expect(
      selectRecommendationCards(
        "1. 純米吟醸生原酒 冬樹FFF\n2. 純米吟醸生原酒 冬樹",
        profile
      ).map((product) => product.id)
    ).toEqual(["fuyuki-fff-nama", "fuyuki-nama"]);
  });
});

function createProfile(products: StoreProduct[]): StoreProfile {
  return {
    store_id: "fukunotomo",
    slug: "fukunotomo",
    display_name: "サンプル店舗",
    service_name: "酒あわせAI",
    headline: "今日の一本を、お好みから一緒に探します",
    description: "正確な価格・在庫は公式オンラインストアで確認してください。",
    location_label: "",
    data_label: "サンプル取扱い酒データ",
    product_count: products.length,
    featured_product_ids: [],
    quick_prompts: { ja: [], en: [], zh: [] },
    next_actions: { ja: [], en: [], zh: [] },
    language_options: [],
    compliance_notes: [],
    products
  };
}

function createProduct(id: string, name: string): StoreProduct {
  return {
    id,
    name,
    brewery_name: "サンプル店舗",
    series: "",
    style_class: "",
    taste_type: "",
    taste_tags: [],
    aroma_tags: [],
    service_methods: [],
    pairing_tags: [],
    reason_tags: [],
    summary: "",
    official_url: "",
    price_label: "",
    stock_status: "needs_verification",
    stock_label: "在庫は公式商品ページで確認ください",
    verified_on: null,
    aliases: [name],
    skus: []
  };
}

