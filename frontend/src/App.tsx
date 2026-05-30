import { AlertCircle, ChevronDown, MessageCircleQuestion, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ChatComposer } from "./components/ChatComposer";
import { ChatMessage } from "./components/ChatMessage";
import { useChat } from "./hooks/useChat";
import { fetchStoreProfile } from "./services/chatApi";
import type { LanguageCode, StoreProfile } from "./types/chat";

function App() {
  const [storeId] = useState(() => getStoreIdFromPath());
  const [language] = useState<LanguageCode>("ja");
  const [storeProfile, setStoreProfile] = useState<StoreProfile | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [contextExpanded, setContextExpanded] = useState(true);
  const {
    clearConversation,
    conversationId,
    error,
    isStreaming,
    messages,
    sendFeedback,
    sendMessage,
    trackProductLinkClick
  } =
    useChat({
      storeId,
      language,
      storeProfile
    });
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const didAutoCollapseContextRef = useRef(false);
  const shouldScrollRestoredConversationRef = useRef(messages.some((message) => message.role === "user"));
  const previousMessageCountRef = useRef(messages.length);
  const shortConversationId = conversationId ? conversationId.slice(-8) : null;
  const quickPrompts = storeProfile?.quick_prompts?.[language] ?? [];
  const hasUserMessage = messages.some((message) => message.role === "user");
  const storeVersionLabel = storeProfile ? `${storeProfile.display_name} ver.` : null;
  const complianceNotes = storeProfile?.compliance_notes ?? [
    "このサイトは非公式ファンサイトです。",
    "AI回答は参考情報です。正確な価格・在庫は公式オンラインストアで確認してください。",
    "20歳未満の飲酒は法律で禁止されています。飲酒は20歳になってから。"
  ];

  useEffect(() => {
    if (
      !shouldScrollRestoredConversationRef.current ||
      !hasUserMessage ||
      contextExpanded
    ) {
      return;
    }

    const timers = [0, 80, 240, 600].map((delay) =>
      window.setTimeout(() => {
        scrollToLastUserMessage(messageListRef.current, "auto");
      }, delay)
    );
    const doneTimer = window.setTimeout(() => {
      shouldScrollRestoredConversationRef.current = false;
    }, 700);

    return () => {
      timers.forEach((timer) => window.clearTimeout(timer));
      window.clearTimeout(doneTimer);
    };
  }, [contextExpanded, hasUserMessage, storeProfile]);

  useEffect(() => {
    if (!hasUserMessage) {
      shouldScrollRestoredConversationRef.current = false;
    }
  }, [hasUserMessage]);

  useEffect(() => {
    const controller = new AbortController();

    async function loadStore() {
      try {
        const profile = await fetchStoreProfile(storeId, controller.signal);
        setStoreProfile(profile);
        setProfileError(null);
      } catch (caught) {
        if (controller.signal.aborted) {
          return;
        }
        setProfileError(caught instanceof Error ? caught.message : "店舗データの取得に失敗しました。");
      }
    }

    void loadStore();

    return () => {
      controller.abort();
    };
  }, [storeId]);

  useEffect(() => {
    if (hasUserMessage && !didAutoCollapseContextRef.current) {
      setContextExpanded(false);
      didAutoCollapseContextRef.current = true;
    }
  }, [hasUserMessage]);

  useEffect(() => {
    const messageList = messageListRef.current;
    const previousMessageCount = previousMessageCountRef.current;
    previousMessageCountRef.current = messages.length;

    if (!messageList) {
      return;
    }
    if (messages.length <= previousMessageCount) {
      return;
    }

    const assistantMessages = messageList.querySelectorAll<HTMLElement>(
      '[data-message-role="assistant"]'
    );
    const lastAssistant = assistantMessages[assistantMessages.length - 1];
    const rows = messageList.querySelectorAll<HTMLElement>(".message-row");
    const fallbackTarget = rows[rows.length - 1];
    const target = lastAssistant ?? fallbackTarget;
    if (!target) {
      return;
    }

    messageList.scrollTo({
      top: getScrollTopForTarget(messageList, target),
      behavior: "smooth"
    });
  }, [messages.length]);

  return (
    <main className="app-shell">
      <section className="chat-panel" aria-label="酒あわせAI">
        <header className="chat-header">
          <div className="brand-mark" aria-hidden="true">
            <span className="janome-mark">
              <span className="janome-mark__center" />
            </span>
          </div>
          <div className="header-copy">
            <h1>
              <span>{storeProfile?.service_name ?? "酒あわせAI"}</span>
              {storeVersionLabel ? <span className="brand-version">{storeVersionLabel}</span> : null}
              <span className="unofficial-badge">※このサイトは非公式ファンサイトです</span>
            </h1>
            <p>
              {shortConversationId ? `相談中 #${shortConversationId}` : "新しい相談"}
            </p>
          </div>
          <div className="header-tools">
            <button
              aria-label="チャット履歴をクリア"
              className="header-clear-button"
              disabled={isStreaming || !hasUserMessage}
              onClick={clearConversation}
              type="button"
            >
              <Trash2 size={16} aria-hidden="true" />
              <span>
                チャット履歴
                <br />
                をクリア
              </span>
            </button>
          </div>
        </header>

        <div ref={messageListRef} className="chat-scroll-region">
          <section className="context-accordion" aria-label="店舗情報と注意事項">
            {hasUserMessage ? (
              <button
                aria-controls="context-accordion-body"
                aria-expanded={contextExpanded}
                className="context-accordion__toggle"
                onClick={() => setContextExpanded((current) => !current)}
                type="button"
              >
                <span>店舗情報・注意事項</span>
                <ChevronDown className="context-accordion__chevron" size={16} aria-hidden="true" />
              </button>
            ) : null}

            <div
              className={`context-accordion__body${hasUserMessage ? " is-accordion" : ""}`}
              hidden={hasUserMessage && !contextExpanded}
              id="context-accordion-body"
            >
              <section className="store-context" aria-label="店舗情報">
                <div className="store-context__copy">
                  <h2>{storeProfile?.headline ?? "今日の一本を、お好みから一緒に探します"}</h2>
                  <p>{storeProfile?.description ?? "正確な価格・在庫は公式オンラインストアで確認してください。"}</p>
                  {profileError ? (
                    <div className="inline-error" role="alert">
                      <AlertCircle size={15} aria-hidden="true" />
                      {profileError}
                    </div>
                  ) : null}
                </div>
              </section>

              <section className="quick-prompt-panel" aria-label="相談ボタン">
                <div className="quick-prompt-panel__label">
                  <MessageCircleQuestion size={15} aria-hidden="true" />
                  <span>例えば、こんなご質問にお答えします！（タップで質問ができます）</span>
                </div>
                <div className="quick-prompts">
                  {quickPrompts.map((prompt) => (
                    <button disabled={isStreaming} key={prompt} onClick={() => void sendMessage(prompt)} type="button">
                      {prompt}
                    </button>
                  ))}
                </div>
              </section>

              <section className="compliance-details" aria-label="利用上の注意">
                <h2>利用上の注意</h2>
                <ul>
                  {complianceNotes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                  <li>評価送信時は、この回答と直前の相談内容を品質改善のため記録します。個人情報、連絡先、住所、健康状態などは入力しないでください。</li>
                  <li>チャット履歴は使いやすさのため、このブラウザに一時保存されます。削除したい場合は「チャット履歴をクリア」を押してください。</li>
                </ul>
              </section>
            </div>
          </section>

          <div className={`message-list${hasUserMessage ? " has-user-message" : ""}`} aria-live="polite">
            {messages.map((message) => (
              <ChatMessage
                actionsDisabled={isStreaming}
                key={message.id}
                message={message}
                onAction={(prompt) => void sendMessage(prompt)}
                onFeedback={(messageId, rating, comment) => sendFeedback(messageId, rating, comment)}
                onProductLinkClick={(messageId, product, recommendationRank) =>
                  trackProductLinkClick(messageId, product, recommendationRank)
                }
              />
            ))}
          </div>
        </div>

        {error ? (
          <div className="error-banner" role="alert">
            <AlertCircle size={18} aria-hidden="true" />
            <span>{error}</span>
          </div>
        ) : null}

        <ChatComposer disabled={isStreaming} onSend={sendMessage} placeholder={getComposerPlaceholder(language)} />
      </section>
    </main>
  );
}

function getStoreIdFromPath(): string {
  const [, prefix, slug] = window.location.pathname.split("/");
  if (prefix === "s" && slug) {
    return slug;
  }
  return "fukunotomo";
}

function scrollToLastUserMessage(
  scrollContainer: HTMLDivElement | null,
  behavior: ScrollBehavior
) {
  if (!scrollContainer) {
    return;
  }
  const userMessages = scrollContainer.querySelectorAll<HTMLElement>('[data-message-role="user"]');
  const lastUser = userMessages[userMessages.length - 1];
  if (!lastUser) {
    return;
  }
  scrollContainer.scrollTo({
    top: getScrollTopForTarget(scrollContainer, lastUser),
    behavior
  });
}

function getScrollTopForTarget(scrollContainer: HTMLElement, target: HTMLElement): number {
  const containerRect = scrollContainer.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  return Math.max(0, scrollContainer.scrollTop + targetRect.top - containerRect.top - 12);
}

function getComposerPlaceholder(language: LanguageCode): string {
  if (language === "en") {
    return "Example: A dry sake for grilled fish around 3,000 yen";
  }
  if (language === "zh") {
    return "例：请推荐适合鱼料理、约3000日元的辛口酒";
  }
  return "例: 甘すぎず、魚料理に合う地域のお酒を教えて";
}

export default App;

