import { ExternalLink, LoaderCircle, SendHorizontal, ThumbsDown, ThumbsUp } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage as ChatMessageType, StoreProduct } from "../types/chat";

type ChatMessageProps = {
  message: ChatMessageType;
  actionsDisabled?: boolean;
  onAction?: (prompt: string) => void;
  onFeedback?: (
    messageId: string,
    rating: "positive" | "negative",
    comment?: string
  ) => Promise<void> | void;
  onProductLinkClick?: (
    messageId: string,
    product: StoreProduct,
    recommendationRank: number
  ) => Promise<void> | void;
};

const safeUrlPattern = /^(https?:|mailto:|\/|#)/i;

export function ChatMessage({
  actionsDisabled = false,
  message,
  onAction,
  onFeedback,
  onProductLinkClick
}: ChatMessageProps) {
  const [feedbackComment, setFeedbackComment] = useState("");
  const [selectedFeedbackRating, setSelectedFeedbackRating] = useState<"positive" | "negative" | null>(
    message.feedback?.rating ?? null
  );
  const isAssistant = message.role === "assistant";
  const label = isAssistant ? "酒あわせAI" : "あなた";
  const activityLabel = message.activityLabel ?? "考えています";
  const showInlineActivity = Boolean(message.content && message.status === "streaming" && message.activityLabel);
  const showFeedback = isAssistant && message.status === "complete" && message.id !== "welcome";
  const feedbackRating = selectedFeedbackRating ?? message.feedback?.rating ?? null;
  const isFeedbackSending = message.feedback?.status === "sending";
  const feedbackSubmitLabel = message.feedback?.status === "sent" ? "コメントを更新" : "コメントを送信";
  const feedbackStatusText =
    message.feedback?.status === "sending"
      ? "送信中です。"
      : message.feedback?.status === "sent"
        ? "フィードバックを記録しました。"
        : message.feedback?.status === "error"
          ? message.feedback.error
          : feedbackRating
            ? "コメントを書いて送信できます。"
            : "評価を選択するとコメントを送信できます。";

  useEffect(() => {
    if (message.feedback?.status === "sent") {
      setFeedbackComment("");
    }
  }, [message.feedback?.status]);

  return (
    <article className={`message-row ${message.role}`} data-message-id={message.id} data-message-role={message.role}>
      <div className="message-meta">{label}</div>
      <div className={`message-bubble ${message.status}`}>
        {message.content ? (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            urlTransform={safeUrlTransform}
            components={{
              a: ({ href, children, ...props }) => (
                <a href={href} rel="noreferrer" target={href?.startsWith("http") ? "_blank" : undefined} {...props}>
                  <span>{children}</span>
                  {href?.startsWith("http") ? <ExternalLink size={14} aria-hidden="true" /> : null}
                </a>
              )
            }}
          >
            {message.content}
          </ReactMarkdown>
        ) : (
          <div className="typing">
            <LoaderCircle className="spin" size={18} aria-hidden="true" />
            <span>{activityLabel}</span>
          </div>
        )}
        {showInlineActivity ? (
          <div className="message-activity" role="status" aria-live="polite">
            <LoaderCircle className="spin" size={14} aria-hidden="true" />
            <span>{activityLabel}</span>
          </div>
        ) : null}
      </div>

      {message.recommendations?.length ? (
        <div className="recommendation-list" aria-label="推薦カード">
          {message.recommendations.map((product, index) => (
            <article className="recommendation-card" key={product.id}>
              <div className="recommendation-card__header">
                <h3>{product.name}</h3>
                <span className={`stock-pill ${product.stock_status}`}>{product.stock_label}</span>
              </div>
              <p>{product.summary}</p>
              <div className="reason-tags">
                {product.reason_tags.slice(0, 5).map((tag) => (
                  <span key={tag}>{tag}</span>
                ))}
              </div>
              <div className="recommendation-card__facts">
                <span>{product.price_label}</span>
                <span>{product.style_class}</span>
                {product.verified_on ? <span>確認日 {product.verified_on}</span> : null}
              </div>
              {product.official_url ? (
                <a
                  className="product-link"
                  href={product.official_url}
                  onClick={() => void onProductLinkClick?.(message.id, product, index + 1)}
                  rel="noreferrer"
                  target="_blank"
                >
                  商品ページ
                  <ExternalLink size={14} aria-hidden="true" />
                </a>
              ) : null}
            </article>
          ))}
        </div>
      ) : null}

      {message.actions?.length ? (
        <div className="next-actions" aria-label="次の一手">
          {message.actions.map((action) => (
            <button disabled={actionsDisabled} key={action} onClick={() => onAction?.(action)} type="button">
              {action}
            </button>
          ))}
        </div>
      ) : null}

      {showFeedback ? (
        <div className="feedback-box" aria-label="回答フィードバック">
          <div className="feedback-actions">
            <button
              aria-pressed={feedbackRating === "positive"}
              disabled={isFeedbackSending}
              onClick={() => setSelectedFeedbackRating("positive")}
              title="役に立った"
              type="button"
            >
              <ThumbsUp size={16} aria-hidden="true" />
              役に立った
            </button>
            <button
              aria-pressed={feedbackRating === "negative"}
              disabled={isFeedbackSending}
              onClick={() => setSelectedFeedbackRating("negative")}
              title="改善したい"
              type="button"
            >
              <ThumbsDown size={16} aria-hidden="true" />
              改善したい
            </button>
          </div>
          <textarea
            aria-label="フィードバックメモ"
            disabled={isFeedbackSending}
            maxLength={280}
            onChange={(event) => setFeedbackComment(event.target.value)}
            placeholder="一言メモ（任意・個人情報は入力しないでください）"
            rows={2}
            value={feedbackComment}
          />
          <p className="feedback-privacy-note">
            評価を送信すると、この回答と直前の相談内容も品質改善のため送信されます。個人情報は入力しないでください。
          </p>
          <div className="feedback-submit-row">
            <button
              className="feedback-submit-button"
              disabled={!feedbackRating || isFeedbackSending}
              onClick={() => {
                if (!feedbackRating) {
                  return;
                }
                void onFeedback?.(message.id, feedbackRating, feedbackComment);
              }}
              type="button"
            >
              <SendHorizontal size={15} aria-hidden="true" />
              {feedbackSubmitLabel}
            </button>
          </div>
          <p className={`feedback-status ${message.feedback?.status ?? ""}`} role="status">
            {feedbackStatusText}
          </p>
        </div>
      ) : null}
    </article>
  );
}

export function safeUrlTransform(url: string): string {
  if (safeUrlPattern.test(url)) {
    return url;
  }

  return "";
}
