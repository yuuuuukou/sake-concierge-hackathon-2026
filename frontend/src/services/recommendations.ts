import type { StoreProduct, StoreProfile } from "../types/chat";

const DEFAULT_MAX_RECOMMENDATION_CARDS = 10;

export function selectRecommendationCards(
  content: string,
  storeProfile: StoreProfile | null,
  maxCards = DEFAULT_MAX_RECOMMENDATION_CARDS
): StoreProduct[] {
  if (!storeProfile || !content.trim()) {
    return [];
  }

  const normalizedContent = normalize(content);
  const scored = storeProfile.products
    .map((product) => ({
      product,
      match: matchProduct(product, normalizedContent)
    }))
    .filter((item): item is { product: StoreProduct; match: ProductMatch } => item.match !== null)
    .sort((a, b) => a.match.firstIndex - b.match.firstIndex || b.match.score - a.match.score)
    .map((item) => item.product);

  return uniqueProducts(scored).slice(0, maxCards);
}

type ProductMatch = {
  firstIndex: number;
  score: number;
};

function matchProduct(product: StoreProduct, normalizedContent: string): ProductMatch | null {
  let bestMatch: ProductMatch | null = null;
  for (const alias of product.aliases) {
    const normalizedAlias = normalize(alias);
    if (!normalizedAlias || normalizedAlias.length < 2) {
      continue;
    }
    let searchStart = 0;
    while (true) {
      const firstIndex = normalizedContent.indexOf(normalizedAlias, searchStart);
      if (firstIndex < 0) {
        break;
      }
      searchStart = firstIndex + 1;
      if (!hasAliasBoundary(normalizedContent, normalizedAlias, firstIndex)) {
        continue;
      }
      const candidate = {
        firstIndex,
        score: normalizedAlias.length
      };
      if (
        !bestMatch ||
        candidate.firstIndex < bestMatch.firstIndex ||
        (candidate.firstIndex === bestMatch.firstIndex && candidate.score > bestMatch.score)
      ) {
        bestMatch = candidate;
      }
    }
  }

  return bestMatch;
}

function hasAliasBoundary(
  normalizedContent: string,
  normalizedAlias: string,
  firstIndex: number
): boolean {
  const endIndex = firstIndex + normalizedAlias.length;
  const nextChar = normalizedContent.slice(endIndex, endIndex + 1);
  if (nextChar && /[a-z0-9]/.test(nextChar)) {
    return /^\d+[.)．、]/.test(normalizedContent.slice(endIndex, endIndex + 5));
  }
  if (nextChar && /[一-龯々〆ヵヶぁ-んァ-ヴー]/.test(nextChar)) {
    return "がをはにでとへもやのか".includes(nextChar);
  }
  return true;
}

function uniqueProducts(products: StoreProduct[]): StoreProduct[] {
  const seen = new Set<string>();
  const unique: StoreProduct[] = [];
  for (const product of products) {
    if (seen.has(product.id)) {
      continue;
    }
    seen.add(product.id);
    unique.push(product);
  }
  return unique;
}

function normalize(value: string): string {
  return value.toLowerCase().replace(/\s+/g, "");
}
