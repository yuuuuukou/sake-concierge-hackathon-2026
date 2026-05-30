import { useCallback, useEffect, useRef, useState } from "react";
import {
  createConversation,
  streamChat,
  submitAnalyticsEvent,
  submitFeedback
} from "../services/chatApi";
import { selectRecommendationCards } from "../services/recommendations";
import type {
  ChatMessage,
  LanguageCode,
  MetricsSummary,
  StoreProduct,
  StoreProfile
} from "../types/chat";

type UseChatOptions = {
  storeId: string;
  language: LanguageCode;
  storeProfile: StoreProfile | null;
  onMetricsUpdate?: (metrics: MetricsSummary) => void;
};

type StoredConversation = {
  version: 1;
  storeId: string;
  language: LanguageCode;
  conversationId: string | null;
  messages: ChatMessage[];
  savedAt: number;
};

const CONVERSATION_STORAGE_VERSION = 1;
const CONVERSATION_STORAGE_TTL_MS = 1000 * 60 * 60 * 24 * 7;
const SESSION_STORAGE_VERSION = 1;
const SESSION_STORAGE_KEY = `sake-concierge:session:v${SESSION_STORAGE_VERSION}`;

export function useChat({ storeId, language, storeProfile, onMetricsUpdate }: UseChatOptions) {
  const storedConversationRef = useRef(loadStoredConversation(storeId, language));
  const [messages, setMessages] = useState<ChatMessage[]>(() =>
    storedConversationRef.current?.messages ?? createInitialMessages(storeProfile, language)
  );
  const [conversationId, setConversationId] = useState<string | null>(
    () => storedConversationRef.current?.conversationId ?? null
  );
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamLabel, setStreamLabel] = useState("待機中");
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const conversationAbortControllerRef = useRef<AbortController | null>(null);
  const conversationIdRef = useRef<string | null>(storedConversationRef.current?.conversationId ?? null);
  const conversationPromiseRef = useRef<Promise<string | null> | null>(null);
  const isStreamingRef = useRef(false);
  const storeProfileRef = useRef<StoreProfile | null>(storeProfile);
  const languageRef = useRef<LanguageCode>(language);
  const sessionIdRef = useRef(getOrCreateSessionId());
  const reportedRecommendationKeysRef = useRef(
    createReportedRecommendationKeys(storedConversationRef.current?.messages ?? [])
  );

  useEffect(() => {
    storeProfileRef.current = storeProfile;
    languageRef.current = language;
    setMessages((current) =>
      current.map((message, index) => {
        if (index === 0 && message.id === "welcome") {
          return createWelcomeMessage(storeProfile, language);
        }
        if (message.role === "assistant" && message.status === "complete" && message.content) {
          return {
            ...message,
            recommendations: selectRecommendationProducts(message, storeProfile),
            actions: getLocalizedActions(storeProfile, language)
          };
        }
        return message;
      })
    );
  }, [language, storeProfile]);

  useEffect(() => {
    saveStoredConversation(storeId, language, {
      conversationId,
      messages
    });
  }, [conversationId, language, messages, storeId]);

  useEffect(() => {
    const activeConversationId = conversationIdRef.current;
    if (!activeConversationId) {
      return;
    }

    for (const message of messages) {
      if (
        message.id === "welcome" ||
        message.role !== "assistant" ||
        message.status !== "complete" ||
        !message.recommendations?.length
      ) {
        continue;
      }

      const reportKey = getRecommendationReportKey(message);
      if (reportedRecommendationKeysRef.current.has(reportKey)) {
        continue;
      }
      reportedRecommendationKeysRef.current.add(reportKey);
      void submitAnalyticsEvent({
        event_type: "recommendation_shown",
        store_id: storeId,
        session_id: sessionIdRef.current,
        conversation_id: activeConversationId,
        message_id: message.id,
        product_ids: message.recommendations.map((product) => product.id),
        page_path: getCurrentPagePath(),
        language: languageRef.current
      }).catch(() => undefined);
    }
  }, [messages, storeId]);

  const rememberConversationId = useCallback((nextConversationId: string) => {
    conversationIdRef.current = nextConversationId;
    setConversationId(nextConversationId);
  }, []);

  const clearConversationId = useCallback(() => {
    conversationIdRef.current = null;
    conversationPromiseRef.current = null;
    setConversationId(null);
  }, []);

  const clearConversation = useCallback(() => {
    abortControllerRef.current?.abort();
    conversationAbortControllerRef.current?.abort();
    clearStoredConversation(storeId, languageRef.current);
    clearConversationId();
    setError(null);
    setIsStreaming(false);
    setStreamLabel("待機中");
    setMessages(createInitialMessages(storeProfileRef.current, languageRef.current));
    isStreamingRef.current = false;
  }, [clearConversationId, storeId]);

  const prepareConversation = useCallback(async (): Promise<string | null> => {
    if (conversationIdRef.current) {
      return conversationIdRef.current;
    }
    if (conversationPromiseRef.current) {
      if (!conversationAbortControllerRef.current?.signal.aborted) {
        return conversationPromiseRef.current;
      }
      conversationPromiseRef.current = null;
    }

    const controller = new AbortController();
    conversationAbortControllerRef.current = controller;
    const promise = createConversation(controller.signal)
      .then((preparedConversationId) => {
        if (!conversationIdRef.current) {
          rememberConversationId(preparedConversationId);
        }
        return conversationIdRef.current ?? preparedConversationId;
      })
      .catch((caught) => {
        if (controller.signal.aborted) {
          return null;
        }
        const messageText =
          caught instanceof Error ? caught.message : "会話の準備に失敗しました。";
        setError(messageText);
        return null;
      })
      .finally(() => {
        if (conversationAbortControllerRef.current === controller) {
          conversationAbortControllerRef.current = null;
        }
        if (conversationPromiseRef.current === promise) {
          conversationPromiseRef.current = null;
        }
      });
    conversationPromiseRef.current = promise;

    return promise;
  }, [rememberConversationId]);

  useEffect(() => {
    void prepareConversation();

    return () => {
      abortControllerRef.current?.abort();
      conversationAbortControllerRef.current?.abort();
    };
  }, [prepareConversation]);

  const sendMessage = useCallback(
    async (rawMessage: string) => {
      const message = rawMessage.trim();
      if (!message || isStreamingRef.current) {
        return;
      }
      isStreamingRef.current = true;

      const userMessage: ChatMessage = {
        id: createId("user"),
        role: "user",
        content: message,
        status: "complete"
      };
      const assistantMessageId = createId("assistant");
      const assistantMessage: ChatMessage = {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        status: "streaming",
        activityLabel: "考えています"
      };

      setMessages((current) => [...current, userMessage, assistantMessage]);
      setError(null);
      setIsStreaming(true);
      setStreamLabel("考えています");

      const abortController = new AbortController();
      abortControllerRef.current = abortController;

      try {
        const preparedConversationId = conversationIdRef.current ?? (await prepareConversation());

        await streamChat(
          {
            message,
            conversation_id: preparedConversationId,
            session_id: sessionIdRef.current,
            store_id: storeId,
            language: languageRef.current
          },
          {
            onMeta: rememberConversationId,
            onDelta: (delta) => {
              updateAssistantMessage(assistantMessageId, (current) => ({
                ...current,
                content: current.content + delta,
                status: "streaming"
              }));
            },
            onStatus: (status) => {
              const nextStatus = status || "考えています";
              setStreamLabel(nextStatus);
              updateAssistantMessage(assistantMessageId, (current) => ({
                ...current,
                activityLabel: nextStatus,
                status: "streaming"
              }));
            },
            onRecommendations: (productIds) => {
              updateAssistantMessage(assistantMessageId, (current) => ({
                ...current,
                recommendationProductIds: productIds,
                recommendations: getProductsByIds(productIds, storeProfileRef.current)
              }));
            },
            onDone: () => {
              updateAssistantMessage(assistantMessageId, (current) => ({
                ...current,
                activityLabel: undefined,
                status: "complete",
                recommendations: selectRecommendationProducts(current, storeProfileRef.current),
                actions: getLocalizedActions(storeProfileRef.current, languageRef.current)
              }));
            },
            onError: (streamError) => {
              throw new Error(streamError);
            }
          },
          abortController.signal
        );
        setError(null);
      } catch (caught) {
        const messageText = caught instanceof Error ? caught.message : "応答の取得に失敗しました。";
        setError(messageText);
        clearConversationId();
        updateAssistantMessage(assistantMessageId, (current) => ({
          ...current,
          content:
            current.content ||
            "応答の取得に失敗しました。少し時間を置いて、もう一度送信してください。",
          status: "error"
        }));
      } finally {
        isStreamingRef.current = false;
        setIsStreaming(false);
        setStreamLabel("待機中");
        abortControllerRef.current = null;
      }
    },
    [clearConversationId, prepareConversation, rememberConversationId, storeId]
  );

  const sendFeedback = useCallback(
    async (messageId: string, rating: "positive" | "negative", comment?: string) => {
      updateAssistantMessage(messageId, (current) => ({
        ...current,
        feedback: {
          rating,
          status: "sending"
        }
      }));

      try {
        const metrics = await submitFeedback({
          store_id: storeId,
          session_id: sessionIdRef.current,
          conversation_id: conversationIdRef.current,
          message_id: messageId,
          rating,
          comment,
          ...getFeedbackContext(messages, messageId),
          language: languageRef.current
        });
        onMetricsUpdate?.(metrics);
        updateAssistantMessage(messageId, (current) => ({
          ...current,
          feedback: {
            rating,
            status: "sent"
          }
        }));
      } catch (caught) {
        const messageText =
          caught instanceof Error ? caught.message : "フィードバック送信に失敗しました。";
        updateAssistantMessage(messageId, (current) => ({
          ...current,
          feedback: {
            rating,
            status: "error",
            error: messageText
          }
        }));
      }
    },
    [messages, onMetricsUpdate, storeId]
  );

  const trackProductLinkClick = useCallback(
    (messageId: string, product: StoreProduct, recommendationRank: number) => {
      void submitAnalyticsEvent({
        event_type: "product_link_clicked",
        store_id: storeId,
        session_id: sessionIdRef.current,
        conversation_id: conversationIdRef.current,
        message_id: messageId,
        product_id: product.id,
        recommendation_rank: recommendationRank,
        official_url: product.official_url,
        page_path: getCurrentPagePath(),
        language: languageRef.current
      }).catch(() => undefined);
    },
    [storeId]
  );

  function updateAssistantMessage(
    messageId: string,
    updater: (message: ChatMessage) => ChatMessage
  ) {
    setMessages((current) =>
      current.map((message) => (message.id === messageId ? updater(message) : message))
    );
  }

  return {
    conversationId,
    error,
    clearConversation,
    isStreaming,
    messages,
    sendFeedback,
    sendMessage,
    trackProductLinkClick,
    streamLabel
  };
}

function getFeedbackContext(messages: ChatMessage[], messageId: string) {
  const assistantIndex = messages.findIndex((message) => message.id === messageId);
  if (assistantIndex < 0) {
    return {};
  }

  const assistantMessage = messages[assistantIndex];
  const userMessage = [...messages.slice(0, assistantIndex)]
    .reverse()
    .find((message) => message.role === "user");

  return {
    user_message: userMessage?.content,
    assistant_message: assistantMessage.content
  };
}

function createInitialMessages(
  storeProfile: StoreProfile | null,
  language: LanguageCode
): ChatMessage[] {
  return [createWelcomeMessage(storeProfile, language)];
}

function createWelcomeMessage(
  storeProfile: StoreProfile | null,
  language: LanguageCode
): ChatMessage {
  const displayName = storeProfile?.display_name ?? "サンプル店舗";

  return {
    id: "welcome",
    role: "assistant",
    status: "complete",
    content:
      language === "en"
        ? `Welcome to Sake Awase AI. Tell me your taste, food, budget, or gift scene and I will help find a bottle from ${displayName}.`
        : language === "zh"
          ? `欢迎使用酒あわせAI。请告诉我口味、料理、预算或送礼场景，我会一起寻找${displayName}的合适一瓶。`
          : `いらっしゃいませ。酒あわせAIです。\nお好みや料理に合わせて、${displayName}の一本を一緒に探します。`
  };
}

function getLocalizedActions(
  storeProfile: StoreProfile | null,
  language: LanguageCode
): string[] | undefined {
  return storeProfile?.next_actions?.[language]?.slice(0, 6);
}

function selectRecommendationProducts(
  message: ChatMessage,
  storeProfile: StoreProfile | null
) {
  if (message.recommendationProductIds?.length) {
    return getProductsByIds(message.recommendationProductIds, storeProfile);
  }
  return selectRecommendationCards(message.content, storeProfile);
}

function getProductsByIds(
  productIds: string[],
  storeProfile: StoreProfile | null
) {
  if (!storeProfile) {
    return [];
  }
  const productsById = new Map(storeProfile.products.map((product) => [product.id, product]));
  return productIds
    .map((productId) => productsById.get(productId))
    .filter((product): product is NonNullable<typeof product> => Boolean(product));
}

function createId(prefix: string): string {
  if (globalThis.crypto?.randomUUID) {
    return `${prefix}-${globalThis.crypto.randomUUID()}`;
  }

  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function getOrCreateSessionId(): string {
  const fallback = createId("session");
  if (!canUseLocalStorage()) {
    return fallback;
  }

  try {
    const storedSessionId = window.localStorage.getItem(SESSION_STORAGE_KEY);
    if (storedSessionId) {
      return storedSessionId;
    }
    window.localStorage.setItem(SESSION_STORAGE_KEY, fallback);
  } catch {
    // Session correlation is best-effort; chat must keep working without storage.
  }
  return fallback;
}

function getCurrentPagePath(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return `${window.location.pathname}${window.location.search}`;
}

function createReportedRecommendationKeys(messages: ChatMessage[]): Set<string> {
  return new Set(
    messages
      .filter(
        (message) =>
          message.role === "assistant" &&
          message.status === "complete" &&
          Boolean(message.recommendations?.length)
      )
      .map(getRecommendationReportKey)
  );
}

function getRecommendationReportKey(message: ChatMessage): string {
  const productIds = message.recommendations?.map((product) => product.id).join(",") ?? "";
  return `${message.id}:${productIds}`;
}

function loadStoredConversation(storeId: string, language: LanguageCode): StoredConversation | null {
  if (!canUseLocalStorage()) {
    return null;
  }

  try {
    const raw = window.localStorage.getItem(getConversationStorageKey(storeId, language));
    if (!raw) {
      return null;
    }

    const parsed = JSON.parse(raw) as Partial<StoredConversation>;
    if (
      parsed.version !== CONVERSATION_STORAGE_VERSION ||
      parsed.storeId !== storeId ||
      parsed.language !== language ||
      typeof parsed.savedAt !== "number" ||
      Date.now() - parsed.savedAt > CONVERSATION_STORAGE_TTL_MS ||
      !Array.isArray(parsed.messages)
    ) {
      window.localStorage.removeItem(getConversationStorageKey(storeId, language));
      return null;
    }

    const messages = parsed.messages
      .filter(isStoredChatMessage)
      .slice(-30)
      .map(normalizeStoredMessage);
    if (messages.length === 0) {
      return null;
    }

    return {
      version: CONVERSATION_STORAGE_VERSION,
      storeId,
      language,
      conversationId: typeof parsed.conversationId === "string" ? parsed.conversationId : null,
      messages,
      savedAt: parsed.savedAt
    };
  } catch {
    return null;
  }
}

function saveStoredConversation(
  storeId: string,
  language: LanguageCode,
  snapshot: Pick<StoredConversation, "conversationId" | "messages">
) {
  if (!canUseLocalStorage()) {
    return;
  }

  try {
    const payload: StoredConversation = {
      version: CONVERSATION_STORAGE_VERSION,
      storeId,
      language,
      conversationId: snapshot.conversationId,
      messages: snapshot.messages.slice(-30),
      savedAt: Date.now()
    };
    window.localStorage.setItem(getConversationStorageKey(storeId, language), JSON.stringify(payload));
  } catch {
    // Local browser memory is a convenience only; chat should keep working if storage is unavailable.
  }
}

function clearStoredConversation(storeId: string, language: LanguageCode): void {
  if (!canUseLocalStorage()) {
    return;
  }
  try {
    window.localStorage.removeItem(getConversationStorageKey(storeId, language));
  } catch {
    // Clearing local browser memory is best-effort only.
  }
}

function getConversationStorageKey(storeId: string, language: LanguageCode): string {
  return `sake-concierge:conversation:v${CONVERSATION_STORAGE_VERSION}:${storeId}:${language}`;
}

function canUseLocalStorage(): boolean {
  try {
    return typeof window !== "undefined" && Boolean(window.localStorage);
  } catch {
    return false;
  }
}

function isStoredChatMessage(value: unknown): value is ChatMessage {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    (value.role === "assistant" || value.role === "user") &&
    typeof value.content === "string" &&
    (value.status === "complete" || value.status === "streaming" || value.status === "error")
  );
}

function normalizeStoredMessage(message: ChatMessage): ChatMessage {
  return {
    ...message,
    activityLabel: undefined,
    status: message.status === "streaming" ? "error" : message.status,
    feedback:
      message.feedback?.status === "sending"
        ? undefined
        : message.feedback
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

