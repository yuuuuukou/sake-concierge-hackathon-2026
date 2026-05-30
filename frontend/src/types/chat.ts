export type ChatRole = "assistant" | "user";

export type ChatMessageStatus = "complete" | "streaming" | "error";

export type LanguageCode = "ja" | "en" | "zh";

export type ProductSku = {
  sku_id: string;
  volume_ml: number | null;
  price_yen: number | null;
  stock_status: string;
  official_url: string;
  verified_on?: string | null;
};

export type StoreProduct = {
  id: string;
  name: string;
  brewery_name: string;
  series: string;
  style_class: string;
  taste_type: string;
  taste_tags: string[];
  aroma_tags: string[];
  service_methods: string[];
  pairing_tags: string[];
  reason_tags: string[];
  summary: string;
  official_url: string;
  price_label: string;
  stock_status: string;
  stock_label: string;
  verified_on?: string | null;
  data_quality_note?: string;
  aliases: string[];
  skus: ProductSku[];
};

export type LanguageOption = {
  code: LanguageCode;
  label: string;
  short_label: string;
};

export type StoreProfile = {
  store_id: string;
  slug: string;
  display_name: string;
  service_name: string;
  headline: string;
  description: string;
  location_label: string;
  data_label: string;
  product_count: number;
  data_updated_on?: string | null;
  featured_product_ids: string[];
  quick_prompts: Record<LanguageCode, string[]>;
  next_actions: Record<LanguageCode, string[]>;
  language_options: LanguageOption[];
  compliance_notes: string[];
  products: StoreProduct[];
};

export type MetricsSummary = {
  store_id: string;
  chat_requests: number;
  feedback: {
    total: number;
    positive: number;
    negative: number;
    positive_ratio: number | null;
  };
  quality_targets: {
    golden_set_pass_rate: string;
    warm_first_delta: string;
    cold_first_delta: string;
  };
  privacy_note: string;
  updated_at: string;
};

export type FeedbackState = {
  rating?: "positive" | "negative";
  status?: "sending" | "sent" | "error";
  error?: string;
};

export type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  status: ChatMessageStatus;
  activityLabel?: string;
  recommendations?: StoreProduct[];
  recommendationProductIds?: string[];
  actions?: string[];
  feedback?: FeedbackState;
};

export type ChatRequest = {
  message: string;
  conversation_id?: string | null;
  session_id?: string | null;
  store_id?: string;
  language?: LanguageCode;
};

export type ConversationResponse = {
  conversation_id: string;
};

export type FeedbackRequest = {
  store_id: string;
  session_id?: string | null;
  conversation_id?: string | null;
  message_id?: string;
  rating: "positive" | "negative";
  comment?: string;
  user_message?: string;
  assistant_message?: string;
  language?: LanguageCode;
};

export type AnalyticsEventType =
  | "message_sent"
  | "recommendation_shown"
  | "product_link_clicked"
  | "feedback_submitted";

export type AnalyticsEventRequest = {
  event_type: AnalyticsEventType;
  store_id: string;
  session_id?: string | null;
  conversation_id?: string | null;
  message_id?: string;
  product_id?: string;
  product_ids?: string[];
  recommendation_rank?: number;
  official_url?: string;
  page_path?: string;
  language?: LanguageCode;
  rating?: "positive" | "negative";
};

export type SseEventName = "meta" | "delta" | "status" | "recommendations" | "done" | "error" | "message";

export type SseEvent = {
  event: SseEventName;
  data: string;
};

export type StreamHandlers = {
  onMeta: (conversationId: string) => void;
  onDelta: (delta: string) => void;
  onRecommendations?: (productIds: string[]) => void;
  onStatus: (status: string) => void;
  onDone: () => void;
  onError: (message: string) => void;
};
