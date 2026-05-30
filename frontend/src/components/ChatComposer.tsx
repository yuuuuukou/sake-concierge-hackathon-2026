import { FormEvent, KeyboardEvent, useRef, useState } from "react";
import { SendHorizontal } from "lucide-react";

const MAX_CHAT_MESSAGE_LENGTH = 1200;

type ChatComposerProps = {
  disabled: boolean;
  onSend: (message: string) => Promise<void>;
  placeholder?: string;
};

export function ChatComposer({ disabled, onSend, placeholder }: ChatComposerProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const canSend = value.trim().length > 0 && !disabled;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend) {
      return;
    }

    const nextMessage = value;
    setValue("");
    await onSend(nextMessage);
    textareaRef.current?.focus();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  return (
    <form className="composer" onSubmit={handleSubmit}>
      <textarea
        ref={textareaRef}
        aria-label="相談内容"
        disabled={disabled}
        maxLength={MAX_CHAT_MESSAGE_LENGTH}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder ?? "例: 甘すぎず、魚料理に合う地域のお酒を教えて"}
        rows={2}
        value={value}
      />
      <button aria-label="送信" disabled={!canSend} title="送信" type="submit">
        <SendHorizontal size={21} strokeWidth={2.3} aria-hidden="true" />
      </button>
    </form>
  );
}

